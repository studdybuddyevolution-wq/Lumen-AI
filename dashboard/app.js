/* Lumen Dashboard — Client-Side Application */
(function () {
    'use strict';

    const API = '';
    let currentPage = 'home';
    let statusInterval = null;
    let executionInterval = null;

    // Navigation
    document.querySelectorAll('.nav-item').forEach(btn => {
        btn.addEventListener('click', () => {
            const page = btn.dataset.page;
            switchPage(page);
        });
    });

    function switchPage(page) {
        currentPage = page;
        document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
        document.querySelector(`[data-page="${page}"]`)?.classList.add('active');
        document.getElementById(`page-${page}`)?.classList.add('active');

        if (page === 'home') {
            refreshStatus();
            refreshExecutions();
        }
        if (page === 'chat') syncChatHistory();
    }

    // ==========================================
    // Status
    // ==========================================
    async function refreshStatus() {
        try {
            const resp = await fetch(`${API}/api/status`);
            const data = await resp.json();
            renderStatus(data);
        } catch (e) {
            console.error('Status fetch failed:', e);
        }
    }

    function renderStatus(data) {
        const grid = document.getElementById('status-grid');
        if (!grid) return;

        const isVoiceOnline = data.voice_process === 'running' || data.bridge === 'connected';

        const items = [
            { label: 'Ollama', value: data.ollama, dot: data.ollama === 'connected' ? 'online' : 'offline' },
            { label: 'Active Model', value: data.active_model || 'qwen3.5:4b', dot: data.ollama === 'connected' ? 'online' : 'offline' },
            { label: 'Voice Runtime', value: isVoiceOnline ? 'running' : 'stopped', dot: isVoiceOnline ? 'online' : 'offline' },
            { label: 'Voice State', value: data.voice || 'offline', dot: data.voice !== 'offline' ? 'online' : 'unknown' },
            { label: 'Speech-to-Text', value: 'faster-whisper (' + (data.whisper || 'ready') + ')', dot: isVoiceOnline ? 'online' : 'unknown' },
            { label: 'Speech Synthesis', value: 'Kokoro (' + (data.kokoro || 'ready') + ')', dot: isVoiceOnline ? 'online' : 'unknown' },
            { label: 'Memory Vault', value: data.memory !== 'not configured' ? 'connected' : 'not configured', dot: data.memory !== 'not configured' ? 'online' : 'offline' },
            { label: 'Barehands Gestures', value: data.barehands || 'stopped', dot: data.barehands === 'running' ? 'online' : 'offline' },
            { label: 'Execution Mode', value: (data.execution_mode || 'autonomous').toUpperCase(), dot: 'online' },
            { label: 'Registered Tools', value: String(data.registered_tools || 0), dot: 'online' },
            { label: 'Feature Modules', value: String(data.feature_modules || 0), dot: 'online' },
            { label: 'Session Turns', value: String(data.turns || 0), dot: 'online' },
        ];

        grid.innerHTML = items.map(i => `
            <div class="status-card">
                <div class="label">${i.label}</div>
                <div class="value">
                    <span class="status-dot ${i.dot}"></span>
                    ${escapeHtml(i.value)}
                </div>
            </div>
        `).join('');
    }

    // ==========================================
    // Tool Execution Activity
    // ==========================================
    const executionList = document.getElementById('execution-list');
    const executionSummary = document.getElementById('execution-summary');
    const executionInspector = document.getElementById('execution-inspector');
    let latestExecutions = [];
    let executionsLoading = false;

    async function refreshExecutions() {
        if (executionsLoading) return;
        executionsLoading = true;
        try {
            const resp = await fetch(`${API}/api/executions?limit=30`, {
                cache: 'no-store',
            });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            renderExecutions(await resp.json());
        } catch (e) {
            console.error('Execution history fetch failed:', e);
        } finally {
            executionsLoading = false;
        }
    }

    function renderExecutions(data) {
        if (!executionList) return;
        latestExecutions = Array.isArray(data.events) ? data.events : [];
        const counts = data.counts || {};
        const running = Number(counts.running || 0);
        const failed = Number(counts.failed || 0);
        const denied = Number(counts.denied || 0);
        if (executionSummary) {
            executionSummary.textContent =
                `${data.count ?? latestExecutions.length} recent · ${running} running · ${failed} failed · ${denied} denied`;
        }

        if (latestExecutions.length === 0) {
            executionList.innerHTML =
                '<div class="empty-state"><p>No tool calls recorded yet.</p></div>';
            return;
        }

        executionList.innerHTML = latestExecutions.map(item => {
            const status = ['running', 'completed', 'failed', 'denied'].includes(item.status)
                ? item.status : 'unknown';
            const provider = ['local', 'cloud'].includes(item.provider)
                ? item.provider : 'unknown';
            const resultText = item.result == null
                ? (status === 'running' ? 'Tool is still running...' : 'No result recorded.')
                : (typeof item.result === 'string'
                    ? item.result : JSON.stringify(item.result));
            const preview = resultText.length > 240
                ? resultText.slice(0, 240) + '…' : resultText;
            const latency = item.latency_ms == null
                ? '—' : `${Number(item.latency_ms).toFixed(1)} ms`;
            const timestamp = formatExecutionTime(item.timestamp);
            const open = status === 'running' ? ' open' : '';

            return `
                <details class="execution-card status-${status}"${open}>
                    <summary>
                        <span class="execution-state-dot ${status}"></span>
                        <span class="execution-name">${escapeHtml(item.name || 'unknown tool')}</span>
                        <span class="execution-provider ${provider}">${provider.toUpperCase()}</span>
                        <span class="execution-status ${status}">${status.toUpperCase()}</span>
                        <span class="execution-meta">${escapeHtml(timestamp)} · ${escapeHtml(latency)}</span>
                    </summary>
                    <div class="execution-card-body">
                        <p class="execution-result">${escapeHtml(preview)}</p>
                        <button class="btn btn-secondary btn-sm"
                            type="button" data-inspect-id="${escapeHtml(item.id)}">
                            Inspect JSON
                        </button>
                    </div>
                </details>
            `;
        }).join('');
    }

    function formatExecutionTime(value) {
        if (!value) return 'Time unavailable';
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? 'Time unavailable'
            : date.toLocaleString();
    }

    function openExecutionInspector(executionId) {
        const item = latestExecutions.find(entry => entry.id === executionId);
        if (!item || !executionInspector) return;
        document.getElementById('execution-inspector-meta').textContent =
            `${item.name} · ${String(item.status).toUpperCase()} · ${formatExecutionTime(item.timestamp)} · ${item.latency_ms == null ? 'running' : Number(item.latency_ms).toFixed(1) + ' ms'} · ${String(item.provider || 'unknown').toUpperCase()}`;
        document.getElementById('execution-inspector-arguments').textContent =
            JSON.stringify(item.args, null, 2) ?? 'null';
        document.getElementById('execution-inspector-result').textContent =
            JSON.stringify(item.result, null, 2) ?? 'null';
        if (!executionInspector.open) executionInspector.showModal();
    }

    executionList?.addEventListener('click', event => {
        const button = event.target.closest('[data-inspect-id]');
        if (button) openExecutionInspector(button.dataset.inspectId);
    });

    document.getElementById('execution-inspector-close')?.addEventListener('click', () => {
        executionInspector?.close();
    });

    executionInspector?.addEventListener('click', event => {
        if (event.target === executionInspector) executionInspector.close();
    });

    // ==========================================
    // Chat (Shared WarmBrain)
    // ==========================================
    const chatMessages = document.getElementById('chat-messages');
    const chatInput = document.getElementById('chat-input');
    const chatSend = document.getElementById('chat-send');
    const chatClear = document.getElementById('chat-clear');

    chatSend?.addEventListener('click', sendMessage);
    chatInput?.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    chatInput?.addEventListener('input', () => {
        chatInput.style.height = 'auto';
        chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + 'px';
    });

    chatClear?.addEventListener('click', async () => {
        chatMessages.innerHTML = `
            <div class="empty-state">
                <div class="icon">💬</div>
                <p>Start a conversation with Lumen</p>
            </div>
        `;
        try {
            await fetch(`${API}/api/clear`, { method: 'POST' });
        } catch (e) {
            console.error('Failed to clear session:', e);
        }
    });

    async function syncChatHistory() {
        try {
            const resp = await fetch(`${API}/api/messages`);
            const data = await resp.json();
            if (data.messages && data.messages.length > 0) {
                chatMessages.innerHTML = '';
                data.messages.forEach(msg => {
                    appendMessage(msg.role, msg.content, false);
                });
            }
        } catch (e) {
            // Bridge might be starting up; ignore
        }
    }

    async function sendMessage() {
        const text = chatInput.value.trim();
        if (!text) return;

        chatInput.value = '';
        chatInput.style.height = 'auto';

        if (chatMessages.querySelector('.empty-state')) {
            chatMessages.innerHTML = '';
        }

        appendMessage('user', text, true);

        const typingId = 'typing-' + Date.now();
        chatMessages.insertAdjacentHTML('beforeend',
            `<div class="chat-msg assistant" id="${typingId}"><span class="spinner"></span> Thinking...</div>`
        );
        chatMessages.scrollTop = chatMessages.scrollHeight;

        try {
            const resp = await fetch(`${API}/api/chat`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: text }),
            });
            const data = await resp.json();
            document.getElementById(typingId)?.remove();

            if (data.error) {
                appendMessage('assistant', `⚠️ ${data.error}`, true);
            } else {
                appendMessage('assistant', data.response || '(No text returned)', true);
            }
        } catch (e) {
            document.getElementById(typingId)?.remove();
            appendMessage('assistant', `Connection error: ${e.message}`, true);
        }
    }

    function appendMessage(role, content, scroll) {
        const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        const div = document.createElement('div');
        div.className = `chat-msg ${role}`;
        div.innerHTML = `${escapeHtml(content)}<div class="meta">${time}</div>`;
        chatMessages.appendChild(div);
        if (scroll) {
            chatMessages.scrollTop = chatMessages.scrollHeight;
        }
    }

    function escapeHtml(text) {
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = String(text);
        return div.innerHTML.replace(/\n/g, '<br>');
    }

    // ==========================================
    // Memory Vault
    // ==========================================
    document.getElementById('memory-search-btn')?.addEventListener('click', searchMemory);
    document.getElementById('memory-query')?.addEventListener('keydown', e => {
        if (e.key === 'Enter') searchMemory();
    });

    document.getElementById('memory-priorities-btn')?.addEventListener('click', async () => {
        try {
            const resp = await fetch(`${API}/api/memory/priorities`);
            const data = await resp.json();
            const results = document.getElementById('memory-results');
            results.innerHTML = `
                <div class="memory-note">
                    <h4>Active Priorities (Active Priorities.md)</h4>
                    <div class="excerpt" style="white-space:pre-wrap;font-family:var(--font);">${escapeHtml(data.content || 'No priorities found.')}</div>
                </div>
            `;
        } catch (e) {
            console.error('Priorities fetch failed:', e);
        }
    });

    document.getElementById('memory-open-obsidian')?.addEventListener('click', async () => {
        try {
            await fetch(`${API}/api/open-obsidian`, { method: 'POST' });
        } catch (e) {
            console.error('Failed to open Obsidian:', e);
        }
    });

    async function searchMemory() {
        const query = document.getElementById('memory-query').value.trim();
        if (!query) return;

        const results = document.getElementById('memory-results');
        results.innerHTML = '<div class="empty-state"><span class="spinner"></span> Searching memory vault...</div>';

        try {
            const resp = await fetch(`${API}/api/memory/search?q=${encodeURIComponent(query)}`);
            const data = await resp.json();

            if (!data.results || data.results.length === 0) {
                results.innerHTML = '<div class="empty-state"><p>No notes found matching your search</p></div>';
                return;
            }

            results.innerHTML = data.results.map(r => `
                <div class="memory-note">
                    <h4>${escapeHtml(r.title)}</h4>
                    <div class="path">${escapeHtml(r.path)}</div>
                    <div class="excerpt">${escapeHtml(r.excerpt)}</div>
                </div>
            `).join('');
        } catch (e) {
            results.innerHTML = '<div class="empty-state"><p>Search error</p></div>';
        }
    }

    // ==========================================
    // Desktop Tools
    // ==========================================
    const apps = [
        { name: 'Notepad', icon: '📝' },
        { name: 'Calculator', icon: '🔢' },
        { name: 'File Explorer', icon: '📁' },
        { name: 'Obsidian', icon: '💎' },
        { name: 'VS Code', icon: '💻' },
        { name: 'Chrome', icon: '🌐' },
        { name: 'Edge', icon: '🔵' },
        { name: 'Task Manager', icon: '📊' },
    ];

    const appGrid = document.getElementById('app-grid');
    if (appGrid) {
        appGrid.innerHTML = apps.map(a => `
            <div class="tool-card" data-app="${a.name}">
                <div class="tool-icon">${a.icon}</div>
                <div class="tool-name">${a.name}</div>
            </div>
        `).join('');

        appGrid.addEventListener('click', async e => {
            const card = e.target.closest('.tool-card');
            if (!card) return;
            const app = card.dataset.app;
            switchPage('chat');
            chatInput.value = `Open ${app}`;
            sendMessage();
        });
    }

    document.getElementById('ps-run')?.addEventListener('click', async () => {
        const cmd = document.getElementById('ps-command').value.trim();
        if (!cmd) return;
        const output = document.getElementById('ps-output');
        output.style.display = 'block';
        output.textContent = 'Submitting command to Lumen runtime...';
        try {
            switchPage('chat');
            chatInput.value = `Run PowerShell: ${cmd}`;
            sendMessage();
        } catch (e) {
            output.textContent = `Error: ${e.message}`;
        }
    });

    // ==========================================
    // Gestures (Barehands)
    // ==========================================
    document.getElementById('gesture-open')?.addEventListener('click', () => {
        window.open('http://127.0.0.1:8794/stage.html', '_blank');
    });

    document.getElementById('gesture-status-btn')?.addEventListener('click', async () => {
        const el = document.getElementById('gesture-status');
        try {
            const resp = await fetch('http://127.0.0.1:8794/state', { cache: 'no-store' });
            if (resp.ok) {
                el.textContent = 'Barehands server is running on http://127.0.0.1:8794 ✓';
                el.style.color = 'var(--success)';
            } else {
                throw new Error('Not responding');
            }
        } catch (e) {
            el.textContent = 'Barehands is not responding on port 8794. Start it from start-lumen.bat.';
            el.style.color = 'var(--danger)';
        }
    });

    // ==========================================
    // Polling Loops
    // ==========================================
    statusInterval = setInterval(() => {
        if (currentPage === 'home') refreshStatus();
    }, 4000);

    executionInterval = setInterval(() => {
        if (currentPage === 'home') refreshExecutions();
    }, 2000);

    // Initial triggers
    refreshStatus();
    refreshExecutions();
    syncChatHistory();
})();
