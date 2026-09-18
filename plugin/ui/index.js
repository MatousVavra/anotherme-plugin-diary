function diaryPlugin() {
    return {
        content: '',
        entryAt: '',
        moodInput: '',
        tagsInput: '',
        timeline: [],
        moodTrend: [],
        saving: false,
        generating: false,
        loadingMore: false,
        hasMore: false,
        audioBlob: null,
        selectedEntry: null,
        entryContent: '',
        pollTimer: null,
        pollAttempts: 0,

        async init() {
            this.entryAt = this.nowISO();
            await Promise.all([this.loadDiaryTimeline(), this.loadMoodTrend()]);
            this.startAnalysisPoll();
            if (AM.voice) {
                AM.voice.onTranscribe('diary', (text, blob) => {
                    this.content = this.content ? this.content + '\n' + text : text;
                    this.audioBlob = blob;
                    AM.toast('Transcribed — Save Entry to keep audio', 'success');
                });
            }
            AM.onCleanup(() => {
                this.stopAnalysisPoll();
                if (AM.voice && AM.voice.isRecording()) AM.voice.stopRecording();
            });
        },

        async saveDiary() {
            const content = this.content.trim();
            if (!content) { AM.toast('Diary is empty', 'warning'); return; }

            this.saving = true;
            const formData = new FormData();
            formData.append('content', content);
            if (this.entryAt) formData.append('entry_at', new Date(this.entryAt).toISOString());
            if (this.moodInput.trim()) formData.append('mood', this.moodInput.trim());
            if (this.tagsInput.trim()) formData.append('tags', this.tagsInput);
            if (this.audioBlob) formData.append('audio', this.audioBlob, 'diary.webm');

            try {
                const resp = await AM.fetch('/plugins/diary', { method: 'POST', body: formData });
                if (!resp) return;
                if (!resp.ok) {
                    AM.toast('Failed to save entry (HTTP ' + resp.status + ')', 'error');
                    return;
                }
                const data = await resp.json();
                AM.toast('Entry saved' + (this.audioBlob ? ' with audio' : '') + ' — analyzing', 'success');
                AM.events.emit('diary_saved', {
                    path: data.path || '',
                    content: content,
                });
                this.content = '';
                this.audioBlob = null;
                this.moodInput = '';
                this.tagsInput = '';
                this.entryAt = this.nowISO();
                await Promise.all([this.loadDiaryTimeline(), this.loadMoodTrend()]);
                this.startAnalysisPoll();
            } catch (e) { AM.toast('Failed: ' + e.message, 'error'); }
            finally { this.saving = false; }
        },

        async draftWithAI() {
            if (this.generating) return;
            this.generating = true;
            const body = {
                include_recent_diary: true,
                mood: this.moodInput.trim() || null,
                tags: this.parseTags(),
            };
            try {
                const resp = await AM.fetch('/plugins/diary/generate', { method: 'POST', body: body });
                if (!resp) return;
                if (!resp.ok) {
                    AM.toast('Failed to draft entry (HTTP ' + resp.status + ')', 'error');
                    return;
                }
                const data = await resp.json();
                AM.toast('Draft created — analyzing', 'success');
                await Promise.all([this.loadDiaryTimeline(), this.loadMoodTrend()]);
                this.startAnalysisPoll();
                const entry = this.timeline.find(e => e.id === data.title);
                if (entry) this.openTimelineEntry(entry);
            } catch (e) { AM.toast('Failed: ' + e.message, 'error'); }
            finally { this.generating = false; }
        },

        async loadDiaryTimeline() {
            try {
                const resp = await AM.fetch('/plugins/diary/timeline');
                if (!resp) return;
                if (!resp.ok) {
                    AM.toast('Failed to load timeline (HTTP ' + resp.status + ')', 'error');
                    return;
                }
                const entries = await resp.json();
                this.timeline = entries;
                this.hasMore = entries.length >= 30;
            } catch (e) {
                console.error('Diary loadTimeline', e);
                AM.toast('Failed to load timeline', 'error');
            }
        },

        async loadMore() {
            if (this.loadingMore || !this.hasMore) return;
            this.loadingMore = true;
            try {
                const resp = await AM.fetch('/plugins/diary/timeline?offset=' + this.timeline.length);
                if (!resp) return;
                if (!resp.ok) {
                    AM.toast('Failed to load older entries (HTTP ' + resp.status + ')', 'error');
                    return;
                }
                const entries = await resp.json();
                const known = new Set(this.timeline.map(e => e.id));
                for (const e of entries) {
                    if (!known.has(e.id)) this.timeline.push(e);
                }
                this.hasMore = entries.length >= 30;
            } catch (e) {
                console.error('Diary loadMore', e);
                AM.toast('Failed to load older entries', 'error');
            }
            finally { this.loadingMore = false; }
        },

        async loadMoodTrend() {
            try {
                const resp = await AM.fetch('/plugins/diary/mood');
                if (!resp) return;
                if (!resp.ok) return;
                const points = await resp.json();
                const byDay = {};
                for (const p of points) {
                    const key = String(p.date || '').slice(0, 10);
                    if (key) byDay[key] = p.mood;
                }
                const days = [];
                const now = new Date();
                for (let i = 29; i >= 0; i--) {
                    const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - i));
                    const key = d.toISOString().slice(0, 10);
                    days.push({ date: key, mood: byDay[key] || '' });
                }
                this.moodTrend = days;
            } catch (e) { console.error('Diary loadMoodTrend', e); }
        },

        startAnalysisPoll() {
            this.stopAnalysisPoll();
            if (!this.timeline.some(e => e.analyzing)) return;
            this.pollAttempts = 0;
            this.pollTimer = setInterval(async () => {
                this.pollAttempts += 1;
                if (this.pollAttempts > 15) { this.stopAnalysisPoll(); return; }
                try {
                    const resp = await AM.fetch('/plugins/diary/timeline');
                    if (!resp || !resp.ok) return;
                    const entries = await resp.json();
                    this.mergeTimeline(entries);
                    if (!entries.some(e => e.analyzing)) {
                        this.stopAnalysisPoll();
                        await Promise.all([this.loadMoodTrend()]);
                        if (this.selectedEntry && this.selectedEntry.analyzing) {
                            const updated = this.timeline.find(e => e.id === this.selectedEntry.id);
                            if (updated && !updated.analyzing) this.openTimelineEntry(updated);
                        }
                    }
                } catch (e) { this.stopAnalysisPoll(); }
            }, 4000);
        },

        stopAnalysisPoll() {
            if (this.pollTimer) {
                clearInterval(this.pollTimer);
                this.pollTimer = null;
            }
        },

        mergeTimeline(entries) {
            const byId = new Map(this.timeline.map(e => [e.id, e]));
            for (const e of entries) byId.set(e.id, e);
            const merged = Array.from(byId.values());
            merged.sort((a, b) => String(b.entry_at || '').localeCompare(String(a.entry_at || '')));
            this.timeline = merged;
        },

        async openTimelineEntry(entry) {
            try {
                const path = 'Diary/' + entry.filename;
                const resp = await AM.fetch('/plugins/notes/' + encodeURIComponent(path));
                if (!resp) return;
                if (resp.status === 404) {
                    AM.toast('Could not read entry — is the notes plugin enabled?', 'error');
                    return;
                }
                if (!resp.ok) {
                    AM.toast('Could not read entry (HTTP ' + resp.status + ')', 'error');
                    return;
                }
                const data = await resp.json();
                this.selectedEntry = entry;
                this.entryContent = data.content;
            } catch (e) { AM.toast('Could not read entry', 'error'); }
        },

        closeTimelineEntry() {
            this.selectedEntry = null;
            this.entryContent = '';
        },

        onReaderClick(e) {
            const link = e.target.closest('.wikilink');
            if (!link) return;
            if (window.AM && AM.notes && typeof AM.notes.open === 'function') {
                AM.notes.open(link.textContent);
            }
        },

        parseTags() {
            return this.tagsInput.split(',').map(t => t.trim()).filter(t => t.length > 0);
        },

        moodColor(mood) {
            if (!mood) return 'var(--border)';
            let h = 0;
            for (let i = 0; i < mood.length; i++) h = (h * 31 + mood.charCodeAt(i)) % 360;
            return 'hsl(' + h + ', 45%, 55%)';
        },

        nowISO() {
            const d = new Date();
            const offset = d.getTimezoneOffset();
            const local = new Date(d.getTime() - offset * 60000);
            return local.toISOString().slice(0, 16);
        },

        truncate(str, len) {
            if (!str) return '';
            return str.length > len ? str.slice(0, len) + '...' : str;
        },
    };
}
