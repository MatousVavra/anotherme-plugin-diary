function diaryPlugin() {
    return {
        content: '',
        entryAt: '',
        timeline: [],
        saving: false,
        audioBlob: null,
        selectedEntry: null,
        entryContent: '',

        async init() {
            this.entryAt = this.nowISO();
            await this.loadDiaryTimeline();
            if (AM.voice) {
                AM.voice.onTranscribe('diary', (text, blob) => {
                    this.content = this.content ? this.content + '\n' + text : text;
                    this.audioBlob = blob;
                    AM.toast('Transcribed — Save Entry to keep audio', 'success');
                });
            }
            AM.onCleanup(() => {
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
            if (this.audioBlob) formData.append('audio', this.audioBlob, 'diary.webm');

            try {
                const resp = await AM.fetch('/plugins/diary', { method: 'POST', body: formData });
                if (!resp) return;
                const data = await resp.json();
                AM.toast('Entry saved' + (this.audioBlob ? ' with audio' : ''), 'success');
                AM.events.emit('diary_saved', {
                    path: data.path || '',
                    content: content,
                });
                this.content = '';
                this.audioBlob = null;
                this.entryAt = this.nowISO();
                await this.loadDiaryTimeline();
            } catch (e) { AM.toast('Failed: ' + e.message, 'error'); }
            finally { this.saving = false; }
        },

        async loadDiaryTimeline() {
            try {
                const resp = await AM.fetch('/plugins/diary/timeline');
                if (resp) this.timeline = await resp.json();
            } catch (e) { console.error('Diary loadTimeline', e); }
        },

        async openTimelineEntry(entry) {
            try {
                const path = 'Diary/' + entry.filename;
                const resp = await AM.fetch('/plugins/notes/' + encodeURIComponent(path));
                if (resp) {
                    const data = await resp.json();
                    this.selectedEntry = entry;
                    this.entryContent = data.content;
                }
            } catch (e) { AM.toast('Could not read entry', 'error'); }
        },

        closeTimelineEntry() {
            this.selectedEntry = null;
            this.entryContent = '';
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
