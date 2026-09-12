import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, UploadFile

from typing import Optional

from pydantic import BaseModel, Field


class NoteType(str, Enum):
    NOTE = "note"
    PROJECT = "project"
    PERSON = "person"
    DIARY = "diary"


class NoteResponse(BaseModel):
    title: str
    path: str
    type: NoteType
    url: str


class DiaryEntryCreate(BaseModel):
    content: str = Field(min_length=1)
    mood: Optional[str] = None
    tags: list[str] = Field(default_factory=list)


class DiaryEntryResponse(NoteResponse):
    mood: Optional[str] = None
    tags: list[str] = Field(default_factory=list)


class DiaryGenerateRequest(BaseModel):
    instructions: Optional[str] = None
    include_recent_diary: bool = False
    mood: Optional[str] = None
    tags: list[str] = Field(default_factory=list)


DIARY_ANALYZE_SYSTEM = """You are AnotherMe's diary analyzer. Use the provided tools to look up related context before extracting structured data.

Respond ONLY with a JSON object:
{
  "mood": "one word or null",
  "tags": ["lowercase-tags"],
  "projects": ["project titles"],
  "people": [{"name": "...", "relationship": "..."}],
  "facts": {"key": "value"},
  "summary": "first-person reflection paragraph with [[wikilinks]]"
}"""


_INTENTION_KEYWORDS = ("plan", "deadline", "goal", "intend", "will", "need to", "want to", "should")
_UNCERTAINTY_KEYWORDS = ("unclear", "unsure", "wonder", "question", "need to figure out")
_EMOTIONAL_KEYWORDS = ("excited", "worried", "frustrated", "relieved", "meaningful", "important")
_CONNECTION_PHRASES = ("reminds me of", "similar to", "connected to")
_OUTCOME_KEYWORDS = ("completed", "finished", "done", "resolved", "decided", "launched", "shipped", "met", "achieved", "closed")


def _strip_json_fence(text: str) -> str:
    """Remove optional markdown JSON fences without assuming newlines."""
    text = (text or "").strip()
    if not text.startswith("```"):
        return text
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def _parse_json_response(text: str) -> dict:
    text = _strip_json_fence(text or "{}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def _get_preview(content: str) -> str:
    for line in content.split("\n"):
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("-") and not line.startswith("**") and not line.startswith("---") and "##" not in line:
            return line[:120]
    return ""


class DiaryApi:
    """Diary domain methods (moved from src/vault.py). Registered as 'diary' API."""

    def __init__(self, vault_manager):
        self._vault = vault_manager

    def create_diary_entry(self, vault_name, content, mood, tags=None, audio_path=None, entry_at=None):
        now = entry_at or datetime.now(timezone.utc)
        ds = now.strftime("%Y-%m-%d")
        ts = now.strftime("%H:%M")
        extra = {"date": ds, "time": ts, "entry_at": now.isoformat(), "audio": audio_path or ""}
        if mood:
            extra["mood"] = mood

        audio_ref = f"\n\n## Audio\n![[{audio_path}]]" if audio_path else ""
        cp = [
            f"# {ds} — {ts}", "",
            "## Raw Transcription", "",
            content, "",
            "## AI Analysis", "",
            "_(analyzing…)_",
            audio_ref
        ]
        if mood:
            cp.insert(2, f"**Mood:** {mood}")
            cp.insert(3, "")

        return self._vault.create_note(vault_name, f"Diary {ds}-{ts.replace(':', '')}-{now.strftime('%f')}", "\n".join(cp), tags, "diary", extra, "Diary")

    def save_diary_audio(self, vault_name, audio_bytes, filename):
        vault = self._vault.vault_path(vault_name)
        audio_dir = vault / "Diary" / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
        ext = Path(filename).suffix or ".webm"
        filepath = audio_dir / f"{ts}{ext}"
        filepath.write_bytes(audio_bytes)
        return str(filepath.relative_to(vault))

    def get_recent_diary(self, vault_name, days=7):
        d = self._vault.vault_path(vault_name) / "Diary"
        if not d.is_dir():
            return ""
        parts = []
        for f in sorted(d.glob("*.md"), reverse=True)[:days]:
            try:
                parts.append(f.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
        return "\n\n".join(parts) if parts else ""

    def get_today_context(self, vault_name):
        vault = self._vault.vault_path(vault_name)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        parts = []
        for folder in ("Diary", "Notes", "Projects"):
            d = vault / folder
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.md"), reverse=True):
                if folder == "Diary" and today not in f.stem:
                    continue
                st = f.stat()
                mt = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d")
                if mt == today or folder == "Diary":
                    try:
                        parts.append(f"--- {folder}/{f.name} ---\n{f.read_text(encoding='utf-8')}")
                    except (OSError, UnicodeDecodeError):
                        pass
        return "\n\n".join(parts) if parts else "(No content recorded today yet)"

    def get_unresolved_threads(self, vault_name):
        """Unresolved threads from recent diary entries. Used by chat."""
        diary_dir = self._vault.vault_path(vault_name) / "Diary"
        if not diary_dir.is_dir():
            return ""
        unresolved = []
        for df in sorted(diary_dir.glob("*.md"), reverse=True)[:10]:
            try:
                content = df.read_text(encoding="utf-8")
                unresolved.extend(self._extract_threads(content))
            except (OSError, UnicodeDecodeError):
                continue
        return "\n".join(f"- {u}" for u in unresolved[:8])

    @staticmethod
    def _extract_threads(content):
        patterns = [
            r"(?:need to|should|must|will|going to|want to|plan to)\s+([^.!\n]+)",
            r"follow.?up(?:\s*[:-]\s*)(.+)",
            r"TODO(?:\s*[:-]\s*)(.+)",
            r"ACTION(?:\s*[:-]\s*)(.+)",
        ]
        found = set()
        for line in content.split("\n"):
            for pat in patterns:
                m = re.search(pat, line, re.IGNORECASE)
                if m:
                    text = m.group(1).strip() if m.lastindex else m.group(0).strip()
                    if len(text) > 5:
                        found.add(text[:120])
        return list(found)[:5]


class Plugin:
    def on_load(self, ctx):
        self._vault = ctx.vault_manager
        self._llm = ctx.llm_client
        self._tools = ctx.tool_executor
        self._ctx_builder = ctx.context_builder
        self._event_bus = ctx.event_bus
        self._ctx = ctx

        self._api = DiaryApi(self._vault)
        ctx.register_api("diary", self._api)

        router = APIRouter()

        # POST /plugins/diary
        @router.post("", status_code=201, response_model=DiaryEntryResponse)
        async def create_diary(
            content: str = Form(..., min_length=1),
            mood: str | None = Form(None),
            tags: str | None = Form(None),
            entry_at: str | None = Form(None),
            audio: UploadFile | None = None,
        ):
            vn = self._ctx.vault_name
            tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

            audio_path = None
            if audio and audio.filename:
                audio_bytes = await audio.read()
                if len(audio_bytes) > 0:
                    audio_path = self._api.save_diary_audio(vn, audio_bytes, audio.filename)

            entry_dt = None
            if entry_at:
                try:
                    entry_dt = datetime.fromisoformat(entry_at.replace("Z", "+00:00"))
                    if entry_dt.tzinfo is None:
                        entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    raise HTTPException(422, "Invalid entry_at timestamp")

            rel = self._api.create_diary_entry(vn, content, mood, tag_list, audio_path, entry_dt)
            asyncio.create_task(self._analyze_and_update(vn, content))
            asyncio.create_task(self._event_bus.emit("diary_saved",
                {"path": str(rel), "content": content, "mood": mood, "tags": tag_list}))
            return DiaryEntryResponse(
                title=rel.stem, path=str(rel), type="diary",
                url=f"/diary/{rel.stem}", mood=mood, tags=tag_list,
            )

        # POST /plugins/diary/generate
        @router.post("/generate", response_model=DiaryEntryResponse)
        async def generate_diary(body: DiaryGenerateRequest):
            vn = self._ctx.vault_name
            rel = self._api.create_diary_entry(vn, "Generating...", body.mood, body.tags or [])
            asyncio.create_task(self._diary_generate_task(vn, body))
            return DiaryEntryResponse(
                title=rel.stem, path=str(rel), type="diary",
                url=f"/diary/{rel.stem}", mood=body.mood, tags=body.tags or [],
            )

        # GET /plugins/diary/latest
        @router.get("/latest")
        def diary_latest():
            vn = self._ctx.vault_name
            vault = self._vault.vault_path(vn)
            diary_dir = vault / "Diary"
            if not diary_dir.is_dir():
                return {"content": "", "analysis": None}
            files = sorted(diary_dir.glob("*.md"), reverse=True)
            if not files:
                return {"content": "", "analysis": None}
            content = files[0].read_text(encoding="utf-8")
            return {"content": content, "analysis": "included"}

        # GET /plugins/diary/timeline
        @router.get("/timeline")
        def diary_timeline():
            vn = self._ctx.vault_name
            vault = self._vault.vault_path(vn)
            diary_dir = vault / "Diary"
            if not diary_dir.is_dir():
                return []
            entries = []
            for f in sorted(diary_dir.glob("*.md"), reverse=True)[:30]:
                content = f.read_text(encoding="utf-8")
                mood = self._vault.extract_frontmatter_field(content, "mood")
                entry_at = self._vault.extract_frontmatter_field(content, "entry_at") or f.stem.replace("Diary ", "").replace("Diary-", "")
                audio = bool(self._vault.extract_frontmatter_field(content, "audio"))
                preview = ""
                for line in content.split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#") and not line.startswith("-") and not line.startswith("**"):
                        if "##" not in line and "---" not in line:
                            preview = line[:120]
                            break
                entries.append({"id": f.stem, "entry_at": entry_at, "date": entry_at, "mood": mood, "preview": preview, "filename": f.name, "audio": audio})
            return entries

        # GET /plugins/diary/mood
        @router.get("/mood")
        def diary_mood():
            vn = self._ctx.vault_name
            vault = self._vault.vault_path(vn)
            diary_dir = vault / "Diary"
            if not diary_dir.is_dir():
                return []
            points = []
            for f in sorted(diary_dir.glob("*.md"))[-90:]:
                content = f.read_text(encoding="utf-8")
                mood = self._vault.extract_frontmatter_field(content, "mood")
                date = self._vault.extract_frontmatter_field(content, "date") or f.stem.replace("Diary ", "").replace("Diary-", "")
                if mood:
                    points.append({"date": date, "mood": mood})
            return points

        # GET /plugins/diary/unified-timeline
        @router.get("/unified-timeline")
        def unified_timeline():
            vn = self._ctx.vault_name
            vault = self._vault.vault_path(vn)
            entries = []

            diary_dir = vault / "Diary"
            if diary_dir.is_dir():
                for f in sorted(diary_dir.glob("*.md"), reverse=True)[:20]:
                    try:
                        content = f.read_text(encoding="utf-8")
                        mood = self._vault.extract_frontmatter_field(content, "mood")
                        entry_at = self._vault.extract_frontmatter_field(content, "entry_at") or f.stem
                        preview = _get_preview(content)
                        entries.append({
                            "type": "diary", "title": f.stem, "date": entry_at,
                            "preview": preview, "mood": mood, "path": str(f.relative_to(vault))
                        })
                    except (OSError, UnicodeDecodeError): continue

            stories_dir = vault / "Stories"
            if stories_dir.is_dir():
                for f in sorted(stories_dir.glob("*.md"), reverse=True)[:20]:
                    try:
                        content = f.read_text(encoding="utf-8")
                        preview = _get_preview(content)
                        st = f.stat()
                        entries.append({
                            "type": "story", "title": f.stem,
                            "date": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                            "preview": preview, "path": str(f.relative_to(vault))
                        })
                    except (OSError, UnicodeDecodeError): continue

            entries.sort(key=lambda e: e.get("date", ""), reverse=True)
            return entries[:30]

        ctx.register_router(router)

        # --- Context providers (Phase 6) ---
        ctx.register_context_provider("chat", self._unresolved_threads_provider)

    def _unresolved_threads_provider(self, user_text, thread_id, vault_name):
        """Return unresolved diary threads for chat context."""
        threads = self._api.get_unresolved_threads(vault_name)
        if threads:
            return "## Unresolved diary threads:\n\n" + threads
        return None

    # --- Analysis pipeline methods ---

    async def analyze_diary_entry(self, entry_text: str, vault_name: str) -> dict:
        messages = self._ctx_builder.build_diary_analyzer_context(entry_text, vault_name)
        messages[0]["content"] += "\n\n" + DIARY_ANALYZE_SYSTEM
        response_text = await self._llm.chat(
            messages=messages,
            tools=self._tools.get_all_tools(),
            vault_manager=self._vault,
            db_module=self._ctx.db_module,
            caller="diary",
        )
        return _parse_json_response(response_text)

    def should_generate_questions(self, analysis: dict) -> bool:
        facts = analysis.get("facts") or {}
        for key in facts:
            lowered = key.lower()
            if any(x in lowered for x in _INTENTION_KEYWORDS):
                return True
        summary = (analysis.get("summary") or "").lower()
        if any(x in summary for x in _UNCERTAINTY_KEYWORDS):
            return True
        if any(x in summary for x in _EMOTIONAL_KEYWORDS):
            return True
        if any(x in summary for x in _CONNECTION_PHRASES):
            return True
        if not any(x in summary for x in _OUTCOME_KEYWORDS):
            if analysis.get("projects"):
                return True
            if analysis.get("people"):
                return True
        return False

    async def generate_follow_up_questions(self, entry_text: str, context_text: str, analysis: dict, model: str | None = None) -> list[str]:
        if not self.should_generate_questions(analysis):
            return []
        prompt = f"""Given this diary entry and its analysis, generate 0 to 2 high-merit follow-up questions the user might want to explore later.

Diary entry:
{entry_text}

Analysis:
{json.dumps(analysis, indent=2)}

Related context:
{context_text}

Rules:
- Only ask if there is a genuine unresolved thread.
- Questions should help recover memory, not interrogate emotions.
- Return ONLY a JSON array of strings. Empty array if no question is merited.
"""
        response_text = await self._llm.chat(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            caller="diary",
        )
        text = _strip_json_fence(response_text or "[]")
        try:
            questions = json.loads(text)
        except json.JSONDecodeError:
            return []
        if not isinstance(questions, list):
            return []
        return [q for q in questions if isinstance(q, str)][:2]

    async def _analyze_and_update(self, vault_name: str, content: str):
        try:
            analysis = await self.analyze_diary_entry(content, vault_name)
            await asyncio.to_thread(self._apply_analysis, vault_name, analysis, content)
            stories_api = self._ctx.get_plugin_api("stories")
            if stories_api:
                await asyncio.to_thread(stories_api.consume_seeds, vault_name, content)

            if self.should_generate_questions(analysis):
                context_text = self._ctx_builder.build_chat_context(content, None, vault_name)
                questions = await self.generate_follow_up_questions(content, context_text, analysis)
                await asyncio.to_thread(self._save_diary_seed_questions, vault_name, questions)

            await asyncio.to_thread(self._update_latest_diary_markdown, vault_name, analysis)
        except Exception:
            logging.getLogger(__name__).exception("Diary analysis failed")
            await asyncio.to_thread(self._clear_analysis_placeholder, vault_name)

    def _apply_analysis(self, vault_name: str, analysis: dict, raw_content: str):
        memory_api = self._ctx.get_plugin_api("memory")
        if memory_api:
            for fact_key, fact_value in (analysis.get("facts") or {}).items():
                if isinstance(fact_value, str) and fact_key and fact_value:
                    memory_api.save_fact(vault_name, fact_key, fact_value)
            for person in analysis.get("people") or []:
                name = person.get("name")
                rel = person.get("relationship")
                if name:
                    memory_api.save_person(vault_name, name, rel, "", [])
        facts_to_save = analysis.get("facts") or {}
        if memory_api and facts_to_save:
            memory_api.update_identity_facts(vault_name, facts_to_save)
        if memory_api:
            for project in analysis.get("projects") or []:
                if project and isinstance(project, str):
                    try:
                        memory_api.save_project(vault_name, project, "", "active")
                    except Exception:
                        pass

    def _save_diary_seed_questions(self, vault_name: str, questions: list[str]):
        if not questions:
            return
        lines = [f"- [ ] {q}" for q in questions]
        self._vault.append_to_note(vault_name, "System/SEEDS.md", "\n" + "\n".join(lines) + "\n")

    def _update_latest_diary_markdown(self, vault_name: str, analysis: dict):
        vault = self._vault.vault_path(vault_name)
        diary_dir = vault / "Diary"
        if not diary_dir.is_dir():
            return
        files = sorted(diary_dir.glob("*.md"), reverse=True)
        for f in files:
            try:
                text = f.read_text(encoding="utf-8")
                if "_(analyzing…)_" not in text:
                    continue
                mood = analysis.get("mood", "")
                tags = analysis.get("tags", [])
                summary = analysis.get("summary", "")
                replacement = ""
                if mood:
                    replacement += f"**Mood:** {mood}\n\n"
                if tags:
                    replacement += f"**Tags:** {', '.join(tags)}\n\n"
                if summary:
                    replacement += summary
                else:
                    replacement += "_No summary extracted._"
                new_text = text.replace("_(analyzing…)_", replacement)
                f.write_text(new_text, encoding="utf-8")
                break
            except (OSError, UnicodeDecodeError):
                continue

    def _clear_analysis_placeholder(self, vault_name: str):
        vault = self._vault.vault_path(vault_name)
        diary_dir = vault / "Diary"
        if not diary_dir.is_dir():
            return
        files = sorted(diary_dir.glob("*.md"), reverse=True)
        for f in files:
            try:
                text = f.read_text(encoding="utf-8")
                if "_(analyzing…)_" in text:
                    new_text = text.replace("_(analyzing…)_", "_Analysis unavailable._")
                    f.write_text(new_text, encoding="utf-8")
                    break
            except (OSError, UnicodeDecodeError):
                continue

    def _replace_generating_placeholder(self, vault_name: str, new_content: str | None):
        vault = self._vault.vault_path(vault_name)
        diary_dir = vault / "Diary"
        if not diary_dir.is_dir():
            return
        files = sorted(diary_dir.glob("*.md"), reverse=True)
        for f in files:
            try:
                text = f.read_text(encoding="utf-8")
                if "Generating..." in text:
                    replacement = new_content if new_content else "_Generation failed._"
                    new_text = text.replace("Generating...", replacement)
                    f.write_text(new_text, encoding="utf-8")
                    break
            except (OSError, UnicodeDecodeError):
                continue

    async def _diary_generate_task(self, vault_name: str, body):
        try:
            today = self._api.get_today_context(vault_name)
            if body.include_recent_diary:
                recent = self._api.get_recent_diary(vault_name)
                today = recent + "\n\n" + today
            prompt = f"Today's context:\n\n{today}\n\nInstructions: Generate a reflective diary entry based on this."
            if body.instructions:
                prompt += f"\n\nAdditional instructions: {body.instructions}"
            generated_text = await self._llm.chat(
                messages=[{"role": "user", "content": prompt}],
                caller="diary",
            )
            await asyncio.to_thread(self._replace_generating_placeholder, vault_name, generated_text)
            await self._analyze_and_update(vault_name, generated_text)
        except Exception:
            logging.getLogger(__name__).exception("Diary generation failed")
            await asyncio.to_thread(self._replace_generating_placeholder, vault_name, None)
