/* chat/app.js — HIVV advanced chat client */
(function () {
  'use strict';

  // ── CDN libs loaded via <script> in HTML; available as globals: marked, hljs, DOMPurify ──
  const HLJS_LANGS = ['python','javascript','typescript','bash','shell','json','html','css','sql','go','rust','java','c','cpp','ruby','php','swift','kotlin','yaml','toml','xml','markdown','dockerfile'];

  // ── STATE ──
  let conversations = JSON.parse(localStorage.getItem('hivv_conversations') || '[]');
  let activeConvId = localStorage.getItem('hivv_active_conv') || null;
  let isGenerating = false;
  let abortController = null;

  // gen params (persisted)
  let gen = loadGen();
  function loadGen() {
    try { return JSON.parse(localStorage.getItem('hivv_gen') || '{}'); } catch { return {}; }
  }
  function saveGen() { localStorage.setItem('hivv_gen', JSON.stringify(gen)); }

  // voice
  let isMuted = localStorage.getItem('hivv_muted') === 'true';
  let currentVoice = localStorage.getItem('hivv_voice') || 'af_bella';
  let currentSpeed = parseFloat(localStorage.getItem('hivv_speed') || '1.0');
  let isVoiceMode = false;
  let isRecording = false;
  let mediaRecorder = null;
  let micStream = null;
  let audioCtx = null;
  let analyser = null;
  let interimTranscription = '';
  let interimTimer = null;
  let vadTimer = null;
  let isSpeaking = false;
  let lastSoundTime = 0;
  let maxRecordingTimer = null;
  let recordedChunks = [];
  let levelAnimId = null;
  let webSpeechRecognition = null;

  // pending files
  let pendingFiles = []; // {file, preview, uploadResult?}

  // ── DOM ──
  const chatArea = document.getElementById('chatArea');
  const welcome = document.getElementById('welcome');
  const input = document.getElementById('chatInput');
  const sendBtn = document.getElementById('sendBtn');
  const voiceToggle = document.getElementById('voiceToggle');
  const voicePanel = document.getElementById('voicePanel');
  const voiceOrb = document.getElementById('voiceOrb');
  const voiceStatus = document.getElementById('voiceStatus');
  const voiceTrans = document.getElementById('voiceTranscript');
  const voiceLevels = document.getElementById('voiceLevels');
  const toastEl = document.getElementById('toast');
  const modelSelect = document.getElementById('modelSelect');
  const modeSelect = document.getElementById('modeSelect');
  const sidebarList = document.getElementById('sidebarList');
  const drawerOverlay = document.getElementById('drawerOverlay');
  const drawer = document.getElementById('drawer');
  const dragOverlay = document.getElementById('dragOverlay');
  const fileChips = document.getElementById('fileChips');
  const fileInput = document.getElementById('fileInput');
  const speedSelect = document.getElementById('speedSelect');
  const voiceSelect = document.getElementById('voiceSelect');

  // ── INIT ──
  function init() {
    if (!conversations.length) createConversation();
    if (!activeConvId || !conversations.find(c => c.id === activeConvId)) {
      activeConvId = conversations[0].id;
    }
    renderSidebar();
    loadConversation(activeConvId);
    loadModelSelects();
    bindDrawerControls();
    setupDragDrop();
    setupClipboardPaste();
    input.focus();
  }

  // ── CONVERSATIONS ──
  function createConversation(title) {
    const c = { id: 'c_' + Date.now(), title: title || 'New chat', messages: [], created: Date.now() };
    conversations.unshift(c);
    activeConvId = c.id;
    saveConversations();
    renderSidebar();
    return c;
  }
  function saveConversations() { localStorage.setItem('hivv_conversations', JSON.stringify(conversations)); }
  function getActive() { return conversations.find(c => c.id === activeConvId); }
  function renderSidebar() {
    sidebarList.innerHTML = '';
    for (const c of conversations) {
      const el = document.createElement('div');
      el.className = 'conv-item' + (c.id === activeConvId ? ' active' : '');
      el.innerHTML = `<span class="conv-title">${escHtml(c.title)}</span><span class="conv-del" data-id="${c.id}" title="Delete">✕</span>`;
      el.addEventListener('click', (e) => {
        if (e.target.classList.contains('conv-del')) { deleteConversation(e.target.dataset.id); return; }
        loadConversation(c.id);
      });
      sidebarList.appendChild(el);
    }
  }
  function loadConversation(id) {
    activeConvId = id;
    localStorage.setItem('hivv_active_conv', id);
    renderSidebar();
    chatArea.innerHTML = '';
    const conv = getActive();
    if (!conv || !conv.messages.length) {
      chatArea.appendChild(welcome);
      welcome.style.display = '';
    } else {
      welcome.style.display = 'none';
      for (const m of conv.messages) {
        if (m.role === 'user') addUserMessage(m.content, m.files);
        else addAIMessage(m.content, m.receipt);
      }
      scrollToBottom();
    }
    input.focus();
  }
  function deleteConversation(id) {
    conversations = conversations.filter(c => c.id !== id);
    if (!conversations.length) createConversation();
    if (activeConvId === id) activeConvId = conversations[0].id;
    saveConversations();
    loadConversation(activeConvId);
  }

  // ── MODEL / MODE SELECTS ──
  function loadModelSelects() {
    modelSelect.value = localStorage.getItem('hivv_model') || 'auto';
    modeSelect.value = localStorage.getItem('hivv_mode') || 'balanced';
    speedSelect.value = localStorage.getItem('hivv_speed') || '1.0';
    voiceSelect.value = localStorage.getItem('hivv_voice') || 'af_bella';
    document.getElementById('muteBtn').textContent = isMuted ? '🔇' : '🔊';
    // drawer controls
    setDrawerVal('temp', gen.temperature ?? 0.7);
    setDrawerVal('topP', gen.top_p ?? 0.95);
    setDrawerVal('maxTokens', gen.max_tokens ?? 2048);
    setDrawerVal('freqPen', gen.frequency_penalty ?? 0);
    setDrawerVal('presPen', gen.presence_penalty ?? 0);
    document.getElementById('sysPrompt').value = gen.system_prompt || '';
    document.getElementById('thinkingToggle').classList.toggle('on', !!gen.thinking);
  }
  function setDrawerVal(id, v) { const el = document.getElementById(id); if (el) el.value = v; updateSliderLabel(id); }
  function updateSliderLabel(id) {
    const el = document.getElementById(id);
    const lbl = document.getElementById(id + 'Val');
    if (el && lbl) lbl.textContent = el.value;
  }
  function bindDrawerControls() {
    for (const id of ['temp','topP','maxTokens','freqPen','presPen']) {
      const el = document.getElementById(id);
      if (el) el.addEventListener('input', () => { updateSliderLabel(id); });
      if (el) el.addEventListener('change', () => { gen[id === 'temp' ? 'temperature' : id === 'topP' ? 'top_p' : id === 'maxTokens' ? 'max_tokens' : id === 'freqPen' ? 'frequency_penalty' : 'presence_penalty'] = parseFloat(el.value); saveGen(); });
    }
    document.getElementById('sysPrompt').addEventListener('change', (e) => { gen.system_prompt = e.target.value; saveGen(); });
    document.getElementById('thinkingToggle').addEventListener('click', (e) => {
      gen.thinking = !gen.thinking;
      e.currentTarget.classList.toggle('on', gen.thinking);
      saveGen();
      showToast(gen.thinking ? '🧠 Thinking ON' : '⚡ Thinking OFF');
    });
    modelSelect.addEventListener('change', () => localStorage.setItem('hivv_model', modelSelect.value));
    modeSelect.addEventListener('change', () => localStorage.setItem('hivv_mode', modeSelect.value));
    speedSelect.addEventListener('change', () => { currentSpeed = parseFloat(speedSelect.value); localStorage.setItem('hivv_speed', currentSpeed); showToast('⚡ Speed: ' + currentSpeed + 'x'); });
    voiceSelect.addEventListener('change', () => { currentVoice = voiceSelect.value; localStorage.setItem('hivv_voice', currentVoice); showToast('🐝 Voice: ' + voiceSelect.options[voiceSelect.selectedIndex].text); });
    document.getElementById('presetCreative').addEventListener('click', () => applyPreset({ temperature: 1.2, top_p: 0.95, frequency_penalty: 0.3 }));
    document.getElementById('presetPrecise').addEventListener('click', () => applyPreset({ temperature: 0.2, top_p: 0.85, frequency_penalty: 0 }));
    document.getElementById('presetBalanced').addEventListener('click', () => applyPreset({ temperature: 0.7, top_p: 0.95, frequency_penalty: 0.1 }));
  }
  function applyPreset(p) {
    Object.assign(gen, p);
    setDrawerVal('temp', gen.temperature);
    setDrawerVal('topP', gen.top_p);
    setDrawerVal('freqPen', gen.frequency_penalty);
    saveGen();
    showToast('✨ Preset applied');
  }

  // ── DRAG & DROP / CLIPBOARD PASTE ──
  function setupDragDrop() {
    let dragCount = 0;
    document.addEventListener('dragenter', (e) => { e.preventDefault(); dragCount++; dragOverlay.classList.add('visible'); });
    document.addEventListener('dragleave', (e) => { e.preventDefault(); dragCount--; if (dragCount <= 0) { dragOverlay.classList.remove('visible'); dragCount = 0; } });
    document.addEventListener('dragover', (e) => e.preventDefault());
    document.addEventListener('drop', (e) => {
      e.preventDefault(); dragCount = 0; dragOverlay.classList.remove('visible');
      if (e.dataTransfer.files.length) addFiles(Array.from(e.dataTransfer.files));
    });
  }
  function setupClipboardPaste() {
    input.addEventListener('paste', (e) => {
      const items = e.clipboardData?.items;
      if (!items) return;
      const files = [];
      for (const item of items) { if (item.kind === 'file') files.push(item.getAsFile()); }
      if (files.length) { e.preventDefault(); addFiles(files.filter(Boolean)); }
    });
  }
  function addFiles(files) {
    for (const file of files) {
      if (file.size > 25 * 1024 * 1024) { showToast('⚠️ File too large (25MB max): ' + file.name); continue; }
      const preview = file.type.startsWith('image/') ? URL.createObjectURL(file) : null;
      pendingFiles.push({ file, preview });
    }
    renderFileChips();
  }
  function renderFileChips() {
    fileChips.innerHTML = '';
    for (let i = 0; i < pendingFiles.length; i++) {
      const f = pendingFiles[i];
      const chip = document.createElement('div');
      chip.className = 'file-chip';
      if (f.preview) chip.innerHTML = `<img class="chip-thumb" src="${f.preview}" alt="">`;
      else chip.innerHTML = `<span class="chip-icon">📄</span>`;
      chip.innerHTML += `<span class="chip-name">${escHtml(f.file.name)}</span><span class="chip-remove" data-i="${i}">✕</span>`;
      chip.querySelector('.chip-remove').addEventListener('click', () => { pendingFiles.splice(i, 1); renderFileChips(); });
      fileChips.appendChild(chip);
    }
  }

  // ── FILE UPLOAD ──
  async function uploadFiles() {
    const results = [];
    for (const f of pendingFiles) {
      const fd = new FormData();
      fd.append('file', f.file);
      try {
        const res = await fetch('/api/files/upload', { method: 'POST', body: fd });
        if (!res.ok) throw new Error('Upload failed: ' + res.status);
        const data = await res.json();
        results.push(data);
      } catch (e) {
        showToast('⚠️ Upload failed: ' + e.message);
      }
    }
    pendingFiles = [];
    renderFileChips();
    return results;
  }

  // ── SEND ──
  async function sendMessage(text) {
    const textToSend = text !== undefined ? text : input.value.trim();
    if (!textToSend && !pendingFiles.length) return;
    if (isGenerating) return;
    input.value = '';
    autoResize(input);
    welcome.style.display = 'none';

    // upload files first
    let uploadedFiles = [];
    if (pendingFiles.length) {
      uploadedFiles = await uploadFiles();
    }

    const conv = getActive();
    addUserMessage(textToSend, uploadedFiles);
    conv.messages.push({ role: 'user', content: textToSend, files: uploadedFiles.length ? uploadedFiles : undefined });
    if (conv.messages.length === 1 && conv.title === 'New chat') {
      conv.title = textToSend.slice(0, 60);
      renderSidebar();
    }
    saveConversations();
    await chatToAI(textToSend, uploadedFiles);
  }

  // ── CHAT → AI ──
  const DEFAULT_SYSTEM = 'You are a helpful, friendly AI assistant on the HIVV distributed network. Keep responses concise and useful. Use markdown when helpful.';

  async function chatToAI(userText, files) {
    isGenerating = true;
    updateSendButton();
    const conv = getActive();
    const sysContent = gen.system_prompt || DEFAULT_SYSTEM;

    // Build messages array
    const apiMessages = [{ role: 'system', content: sysContent }];
    for (const m of conv.messages) {
      apiMessages.push({ role: m.role, content: m.content });
    }

    // If files were uploaded, add context
    let contextAddendum = '';
    if (files && files.length) {
      for (const f of files) {
        if (f.text) contextAddendum += `\n\n--- File: ${f.name} (${f.chars} chars) ---\n${f.text}\n--- End: ${f.name} ---`;
        else if (f.kind === 'image') contextAddendum += `\n\n[Image uploaded: ${f.name} (${f.mime}, ${Math.round(f.size/1024)}KB)]`;
        else contextAddendum += `\n\n[File uploaded: ${f.name} (${f.kind}, ${Math.round(f.size/1024)}KB)]`;
      }
    }

    // Build the final user message with vision content if images exist
    const imageFiles = (files || []).filter(f => f.kind === 'image');
    let userContent;
    if (imageFiles.length) {
      const content = [];
      if (userText) content.push({ type: 'text', text: userText + contextAddendum });
      else content.push({ type: 'text', text: contextAddendum || 'Please describe this image.' });
      for (const img of imageFiles) {
        const res = await fetch(img.url);
        const blob = await res.blob();
        const dataUrl = await blobToDataUrl(blob);
        content.push({ type: 'image_url', image_url: { url: dataUrl } });
      }
      userContent = content;
    } else {
      userContent = userText + contextAddendum;
    }

    apiMessages.push({ role: 'user', content: userContent });

    const msgEl = addAIMessage('');

    try {
      abortController = new AbortController();
      const body = {
        messages: apiMessages,
        model: modelSelect.value,
        stream: true,
      };
      // attach gen params
      if (gen.temperature != null) body.temperature = gen.temperature;
      if (gen.top_p != null) body.top_p = gen.top_p;
      if (gen.max_tokens) body.max_tokens = gen.max_tokens;
      if (gen.frequency_penalty != null) body.frequency_penalty = gen.frequency_penalty;
      if (gen.presence_penalty != null) body.presence_penalty = gen.presence_penalty;
      if (gen.thinking) body.thinking = true;

      const stream = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: abortController.signal,
      });
      if (!stream.ok) throw new Error('API error: ' + stream.status);

      const receipt = stream.headers.get('X-Hivv-Answered-By') || '';
      const policyReceipt = stream.headers.get('X-Hivv-Policy') || '';
      const reader = stream.body.getReader();
      const decoder = new TextDecoder();

      let full = '';
      const bubble = msgEl.querySelector('.bubble');
      let renderDebounce = null;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value);
        const lines = chunk.split('\n');
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const data = line.slice(6);
          if (data === '[DONE]') continue;
          try {
            const parsed = JSON.parse(data);
            const content = parsed.choices?.[0]?.delta?.content;
            if (content) {
              full += content;
              // Debounced render for performance
              if (!renderDebounce) {
                renderDebounce = setTimeout(() => {
                  renderMarkdown(bubble, full);
                  scrollToBottom();
                  renderDebounce = null;
                }, 50);
              }
            }
            // Check for stats — top-level hivv_stats frame (OpenCode-safe: no
            // choices/finish_reason) with legacy finish_reason:"stats" fallback
            const stats = parsed.hivv_stats
                || (parsed.choices?.[0]?.finish_reason === 'stats' ? parsed.choices[0].hivv_stats : null);
            if (stats) {
                const receiptEl = msgEl.querySelector('.receipt');
                if (receiptEl) {
                  const receiptText = receipt || receiptEl.dataset.receipt || '';
                  receiptEl.innerHTML = `<span class="dot"></span>${escHtml(receiptText)} · ${stats.tokens_per_sec}t/s · ${stats.total_tokens}tok`;
                }
              }
          } catch {}
        }
      }
      if (renderDebounce) clearTimeout(renderDebounce);
      renderMarkdown(bubble, full);
      scrollToBottom();

      // Set receipt
      if (receipt) {
        const receiptEl = msgEl.querySelector('.receipt');
        if (receiptEl) {
          receiptEl.dataset.receipt = receipt;
          receiptEl.innerHTML = `<span class="dot"></span>${escHtml(receipt)}${policyReceipt ? ' · ' + escHtml(policyReceipt) : ''}`;
        }
      }

      if (full.trim()) {
        conv.messages.push({ role: 'assistant', content: full, receipt });
        saveConversations();
        if (!isMuted && full.length < 3000) playTTS(full);
      }
    } catch (err) {
      const bubble = msgEl.querySelector('.bubble');
      renderMarkdown(bubble, full || '⚠️ Error: ' + err.message);
      if (!full) bubble.style.color = '#ff5252';
    }

    isGenerating = false;
    updateSendButton();
    scrollToBottom();
  }

  // ── MARKDOWN RENDERING ──
  function renderMarkdown(el, text) {
    if (!text) { el.innerHTML = ''; return; }
    const raw = marked.parse(text, { breaks: true, gfm: true });
    const clean = DOMPurify.sanitize(raw, { ADD_ATTR: ['target'] });
    el.innerHTML = clean;
    // Post-process: wrap code blocks with header + line numbers + actions
    el.querySelectorAll('pre code').forEach(block => {
      const pre = block.parentElement;
      // Detect language
      const cls = Array.from(block.classList).find(c => c.startsWith('language-'));
      const lang = cls ? cls.replace('language-', '') : '';
      // Highlight
      if (lang && hljs.getLanguage(lang)) {
        block.innerHTML = hljs.highlight(block.textContent, { language: lang }).value;
      } else {
        try { block.innerHTML = hljs.highlightAuto(block.textContent).value; } catch {}
      }
      // Build header
      const header = document.createElement('div');
      header.className = 'code-header';
      header.innerHTML = `<span class="code-lang">${escHtml(lang || 'code')}</span>
        <div class="code-actions">
          <button class="code-action-btn copy-code" title="Copy">📋 Copy</button>
          <button class="code-action-btn download-code" title="Download">⬇</button>
        </div>`;
      // Line numbers
      const lineCount = block.textContent.split('\n').length;
      const nums = document.createElement('div');
      nums.className = 'line-numbers';
      for (let i = 1; i <= lineCount; i++) {
        nums.innerHTML += `<span class="line-number">${i}</span>`;
      }
      // Wrap content
      const body = document.createElement('div');
      body.className = 'code-body';
      body.appendChild(nums);
      body.appendChild(block);
      pre.innerHTML = '';
      pre.appendChild(header);
      pre.appendChild(body);
      // Copy button
      header.querySelector('.copy-code').addEventListener('click', (e) => {
        navigator.clipboard.writeText(block.textContent).then(() => {
          e.target.textContent = '✓ Copied';
          e.target.classList.add('copied');
          setTimeout(() => { e.target.textContent = '📋 Copy'; e.target.classList.remove('copied'); }, 2000);
        });
      });
      // Download button
      header.querySelector('.download-code').addEventListener('click', () => {
        const ext = lang === 'python' ? 'py' : lang === 'javascript' ? 'js' : lang === 'typescript' ? 'ts' : lang === 'shell' || lang === 'bash' ? 'sh' : lang || 'txt';
        const blob = new Blob([block.textContent], { type: 'text/plain' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'code.' + ext;
        a.click();
      });
    });
  }

  // ── MESSAGE ELEMENTS ──
  function addUserMessage(text, files) {
    const el = document.createElement('div');
    el.className = 'message user';
    let fileHtml = '';
    if (files && files.length) {
      fileHtml = '<div style="margin-top:4px;font-size:0.75rem;color:#011a0f;opacity:0.7">' + files.map(f => '📎 ' + escHtml(f.name)).join(' · ') + '</div>';
    }
    el.innerHTML = `
      <div class="msg-avatar user-avatar">👤</div>
      <div class="msg-content">
        <div class="bubble user">${escHtml(text)}${fileHtml}</div>
        <div class="msg-actions">
          <button class="msg-action-btn" onclick="window._chatCopy(this)" title="Copy">📋</button>
        </div>
      </div>`;
    chatArea.appendChild(el);
    scrollToBottom();
    return el;
  }

  function addAIMessage(text, receipt) {
    const el = document.createElement('div');
    el.className = 'message ai';
    el.innerHTML = `
      <div class="msg-avatar ai">🐝</div>
      <div class="msg-content">
        <div class="bubble ai">${escHtml(text)}</div>
        <div class="receipt" data-receipt="${escHtml(receipt || '')}">${receipt ? '<span class="dot"></span>' + escHtml(receipt) : ''}</div>
        <div class="msg-actions">
          <button class="msg-action-btn" onclick="window._chatCopy(this)" title="Copy">📋</button>
          <button class="msg-action-btn" onclick="window._chatTTS(this)" title="Read aloud">🔊</button>
        </div>
      </div>`;
    chatArea.appendChild(el);
    scrollToBottom();
    return el;
  }

  function addThinkingMessage() {
    const el = document.createElement('div');
    el.className = 'thinking ai';
    el.innerHTML = `
      <div class="msg-avatar ai">🐝</div>
      <div class="thinking-bubble">
        <div class="thinking-dots"><span></span><span></span><span></span></div>
        <span>HIVV is thinking…</span>
      </div>`;
    chatArea.appendChild(el);
    scrollToBottom();
    return el;
  }

  // ── TTS ──
  // Global AudioContext created on first user interaction (bypasses autoplay policy)
  let ttsAudioCtx = null;
  document.addEventListener('click', () => {
    if (!ttsAudioCtx) { try { ttsAudioCtx = new AudioContext(); } catch {} }
  }, { once: true });
  document.addEventListener('keydown', () => {
    if (!ttsAudioCtx) { try { ttsAudioCtx = new AudioContext(); } catch {} }
  }, { once: true });

  async function playTTS(el) {
    const bubble = el.classList ? el.closest('.message')?.querySelector('.bubble') : null;
    const raw = bubble ? bubble.textContent : (typeof el === 'string' ? el : '');
    const text = raw.replace(/[^\x20-\x7E\n]/g, ' ').replace(/[*_`#>~\[\]]/g, '').replace(/\s+/g, ' ').trim();
    if (!text) return;
    try {
      const res = await fetch('/api/tts', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, voice: currentVoice, speed: currentSpeed })
      });
      if (!res.ok) throw new Error('TTS service returned ' + res.status);
      const blob = await res.blob();
      const arrayBuffer = await blob.arrayBuffer();
      // Use AudioContext to bypass autoplay policy
      if (!ttsAudioCtx) { try { ttsAudioCtx = new AudioContext(); } catch {} }
      if (ttsAudioCtx) {
        await ttsAudioCtx.resume();
        const audioBuffer = await ttsAudioCtx.decodeAudioData(arrayBuffer);
        const source = ttsAudioCtx.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(ttsAudioCtx.destination);
        source.start();
        showToast('🔊 Speaking…');
      } else {
        // Fallback: plain Audio element (may be blocked by autoplay)
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        await audio.play();
        showToast('🔊 Playing…');
      }
    } catch (e) { showToast('⚠️ TTS: ' + e.message); }
  }

  // ── VOICE ──
  function toggleVoiceMode() {
    if (isGenerating) return;
    isVoiceMode ? stopVoiceRecognition() : startVoiceRecognition();
  }
  async function startVoiceRecognition() {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
      micStream = stream;
      audioCtx = new AudioContext();
      analyser = audioCtx.createAnalyser();
      analyser.fftSize = 64;
      const source = audioCtx.createMediaStreamSource(stream);
      source.connect(analyser);
      animateLevels();
      recordedChunks = [];
      mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm;codecs=opus' });
      mediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) recordedChunks.push(e.data); };
      mediaRecorder.onstop = async () => {
        if (recordedChunks.length === 0) return;
        voiceStatus.textContent = '🎤 Transcribing…';
        const blob = new Blob(recordedChunks, { type: 'audio/webm' });
        recordedChunks = [];
        try {
          const res = await fetch('/api/stt', { method: 'POST', body: blob });
          const data = await res.json();
          if (data.text && data.text.trim()) {
            input.value = data.text.trim();
            autoResize(input);
            voiceTrans.textContent = data.text.trim();
            voiceTrans.className = 'voice-transcript';
            setTimeout(() => { stopVoiceRecognition(); sendMessage(); }, 300);
          } else if (interimTranscription) {
            input.value = interimTranscription;
            autoResize(input);
            setTimeout(() => { stopVoiceRecognition(); sendMessage(); }, 300);
          } else {
            showToast('🎤 No speech detected');
            voiceStatus.textContent = 'Listening…';
          }
        } catch (e) {
          if (interimTranscription) { input.value = interimTranscription; setTimeout(() => { stopVoiceRecognition(); sendMessage(); }, 300); }
          else showToast('🎤 Transcription failed');
        }
        interimTranscription = '';
      };
      mediaRecorder.start(100);
      isVoiceMode = true; isRecording = true;
      voicePanel.classList.add('active');
      voiceToggle.classList.add('listening');
      voiceStatus.textContent = 'Listening…';
      voiceTrans.textContent = '';
      // Web Speech interim
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      webSpeechRecognition = null;
      if (SpeechRecognition) {
        try {
          webSpeechRecognition = new SpeechRecognition();
          webSpeechRecognition.continuous = true;
          webSpeechRecognition.interimResults = true;
          webSpeechRecognition.lang = 'en-US';
          webSpeechRecognition.onresult = (e) => {
            let interim = '', final = '';
            for (let i = e.resultIndex; i < e.results.length; i++) {
              const t = e.results[i][0].transcript;
              if (e.results[i].isFinal) final += t; else interim += t;
            }
            if (final) interimTranscription = final.trim();
            else if (interim) interimTranscription = interim.trim();
            if (interimTranscription) {
              voiceTrans.textContent = interimTranscription;
              voiceTrans.className = 'voice-transcript interim';
              voiceStatus.textContent = '✨ Tap to send';
            }
          };
          webSpeechRecognition.onerror = (e) => {
            // Chrome's Web Speech API requires Google servers — "network" error
            // is common when offline or behind strict firewalls. The server-side
            // STT (MediaRecorder) is the primary path; Web Speech is just interim.
            if (e.error === 'network') {
              // Silently fall back to server STT — don't alarm the user
              try { webSpeechRecognition.stop(); } catch {}
            }
          };
          webSpeechRecognition.start();
        } catch { webSpeechRecognition = null; }
      }
      // VAD
      const VAD_SILENCE_THRESHOLD = 4, VAD_SILENCE_MS = 250, MAX_RECORDING_MS = 30000;
      isSpeaking = false; lastSoundTime = Date.now(); interimTranscription = '';
      function checkVAD() {
        if (!isRecording || !analyser) return;
        const data = new Uint8Array(analyser.frequencyBinCount);
        analyser.getByteFrequencyData(data);
        const avg = data.reduce((a, b) => a + b, 0) / data.length;
        if (avg > VAD_SILENCE_THRESHOLD) { isSpeaking = true; lastSoundTime = Date.now(); if (vadTimer) { clearTimeout(vadTimer); vadTimer = null; } }
        else if (!vadTimer) { vadTimer = setTimeout(() => { if (isRecording && isSpeaking) forceStop(); }, VAD_SILENCE_MS); }
        if (isRecording) requestAnimationFrame(checkVAD);
      }
      requestAnimationFrame(checkVAD);
      maxRecordingTimer = setTimeout(forceStop, MAX_RECORDING_MS);
    } catch (err) {
      showToast('🎤 Mic unavailable: ' + (err.name === 'NotAllowedError' ? 'permission denied' : 'not found'));
      isVoiceMode = false;
    }
  }
  function forceStop() {
    if (!isRecording) return;
    isRecording = false; isSpeaking = false;
    if (interimTimer) { clearInterval(interimTimer); interimTimer = null; }
    if (vadTimer) { clearTimeout(vadTimer); vadTimer = null; }
    if (maxRecordingTimer) { clearTimeout(maxRecordingTimer); maxRecordingTimer = null; }
    const rec = mediaRecorder;
    if (interimTranscription && rec && rec.state !== 'inactive') {
      input.value = interimTranscription; autoResize(input);
      voiceTrans.textContent = interimTranscription;
      voiceStatus.textContent = '✨ Sending…';
      rec.onstop = null; rec.stop();
      stopVoiceRecognition(); sendMessage();
    } else if (rec && rec.state !== 'inactive') { rec.stop(); }
  }
  function stopVoiceRecognition() {
    isVoiceMode = false; isRecording = false; isSpeaking = false; interimTranscription = '';
    if (interimTimer) { clearInterval(interimTimer); interimTimer = null; }
    if (vadTimer) { clearTimeout(vadTimer); vadTimer = null; }
    if (maxRecordingTimer) { clearTimeout(maxRecordingTimer); maxRecordingTimer = null; }
    if (webSpeechRecognition) { try { webSpeechRecognition.abort(); } catch {} webSpeechRecognition = null; }
    if (mediaRecorder && mediaRecorder.state !== 'inactive') { try { mediaRecorder.stop(); } catch {} }
    mediaRecorder = null;
    if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
    if (audioCtx) { audioCtx.close(); audioCtx = null; }
    voicePanel.classList.remove('active');
    voiceToggle.classList.remove('listening');
    stopLevelAnimation();
  }
  function animateLevels() {
    if (!analyser) return;
    const data = new Uint8Array(analyser.frequencyBinCount);
    analyser.getByteFrequencyData(data);
    const bars = voiceLevels.querySelectorAll('span');
    const step = Math.floor(data.length / bars.length);
    bars.forEach((bar, i) => {
      const v = data[i * step] || 0;
      bar.style.height = Math.max(4, (v / 255) * 36) + 'px';
      bar.style.opacity = v > 10 ? '1' : '0.3';
    });
    levelAnimId = requestAnimationFrame(animateLevels);
  }
  function stopLevelAnimation() {
    if (levelAnimId) { cancelAnimationFrame(levelAnimId); levelAnimId = null; }
    voiceLevels.querySelectorAll('span').forEach(s => { s.style.height = '6px'; s.style.opacity = '0.3'; });
  }

  // ── UTILS ──
  function escHtml(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
  function scrollToBottom() { requestAnimationFrame(() => { chatArea.scrollTop = chatArea.scrollHeight; }); }
  function autoResize(el) { el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 160) + 'px'; }
  function blobToDataUrl(blob) { return new Promise((resolve) => { const r = new FileReader(); r.onload = () => resolve(r.result); r.readAsDataURL(blob); }); }

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  }
  function updateSendButton() {
    sendBtn.innerHTML = isGenerating ? '<span style="font-size:.7rem">■</span>' : '➤';
    sendBtn.onclick = isGenerating ? () => { if (abortController) abortController.abort(); isGenerating = false; updateSendButton(); } : sendMessage;
    sendBtn.className = isGenerating ? 'stop-btn' : 'send-btn';
  }
  function showToast(msg) {
    toastEl.textContent = msg; toastEl.classList.add('show');
    clearTimeout(window._toastTimer);
    window._toastTimer = setTimeout(() => toastEl.classList.remove('show'), 2500);
  }
  function toggleMute() {
    isMuted = !isMuted; localStorage.setItem('hivv_muted', isMuted);
    document.getElementById('muteBtn').textContent = isMuted ? '🔇' : '🔊';
    showToast(isMuted ? '🔇 Muted' : '🔊 Voice on');
  }
  function clearChat() {
    const conv = getActive();
    conv.messages = []; saveConversations();
    chatArea.innerHTML = ''; chatArea.appendChild(welcome); welcome.style.display = '';
    input.value = ''; autoResize(input); input.focus();
  }
  function exportChat() {
    const conv = getActive();
    if (!conv || !conv.messages.length) return showToast('Nothing to export');
    const text = conv.messages.map(m => `**${m.role}:** ${m.content}`).join('\n\n');
    const blob = new Blob([text], { type: 'text/plain' });
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
    a.download = 'hivv-chat-' + Date.now() + '.txt'; a.click();
    showToast('💾 Exported');
  }
  function sendSuggestion(el) { sendMessage(el.textContent.trim()); }

  // ── DRAWER ──
  function openDrawer() { drawerOverlay.classList.add('open'); drawer.classList.add('open'); }
  function closeDrawer() { drawerOverlay.classList.remove('open'); drawer.classList.remove('open'); }

  // ── KEYBOARD SHORTCUTS ──
  document.addEventListener('keydown', (e) => {
    if (e.key === 'v' && !e.ctrlKey && !e.metaKey && !e.altKey && document.activeElement !== input && document.activeElement.tagName !== 'TEXTAREA' && document.activeElement.tagName !== 'INPUT') toggleVoiceMode();
    if (e.key === 'm' && !e.ctrlKey && !e.metaKey && !e.altKey && document.activeElement !== input && document.activeElement.tagName !== 'TEXTAREA' && document.activeElement.tagName !== 'INPUT') toggleMute();
  });

  // ── GLOBAL HANDLERS (for inline onclick) ──
  window._chatCopy = function(btn) { const text = btn.closest('.message').querySelector('.bubble').textContent; navigator.clipboard.writeText(text).then(() => showToast('📋 Copied!')); };
  window._chatTTS = function(btn) { playTTS(btn); };
  window.sendMessage = sendMessage;
  window.toggleVoiceMode = toggleVoiceMode;
  window.clearChat = clearChat;
  window.exportChat = exportChat;
  window.sendSuggestion = sendSuggestion;
  window.autoResize = autoResize;
  window.handleKeyDown = handleKeyDown;
  window.toggleMute = toggleMute;
  window.openDrawer = openDrawer;
  window.closeDrawer = closeDrawer;
  window._addFiles = addFiles;

  init();
})();
