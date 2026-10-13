(function () {
  const page = document.body.dataset.page;
  const bookId = document.body.dataset.bookId;
  const stageLabels = {
    precheck: "检查朗读稿",
    synthesize: "生成并试听语音",
    package: "导出章节文件",
  };
  const statusLabels = {
    uploaded: "等待本地解析",
    parsing: "正在建立索引",
    ready: "可以开始制作",
    attention: "有问题需要处理",
    executing: "正在执行",
    completed: "已完成",
    failed: "解析失败",
  };
  const executionStatusLabels = {
    queued: "等待执行",
    running: "执行中",
    completed: "已完成",
    failed: "需要处理",
    paused: "已暂停",
  };
  const stageOrder = ["precheck", "synthesize", "package"];
  const workbench = {
    mode: "chapters",
    chapterStart: 1,
    chapterEnd: 1,
    targetCharacters: 5000,
    startBatch: 1,
    endBatch: 1,
    startSegment: 1,
    endSegment: 1,
    advancedUnit: "batch",
    chapterPendingStart: null,
    lockedRange: null,
    rangeConfirmed: false,
    rangePreview: null,
    stagePreviews: {},
    previewError: "",
    view: "workflow",
    timer: null,
    previewRequestId: 0,
    latestData: null,
    downloadChapterIds: new Set(),
  };
  let ttsProviders = [];
  const CLONE_SECONDS_HINT = "3～20 秒";
  // 设计音色时的声音描述和样音文本都会影响生成音色的语气和节奏，按书籍题材各配一套；样音需要落在 3～20 秒，文本控制在 50 字以内
  const DESIGN_SAMPLE_TEXTS = [
    { label: "通用叙事", description: "四十岁左右的男性旁白，嗓音温厚清晰，语速适中，语气平和自然，适合长时间收听", text: "夜色渐深，老街上的灯一盏盏亮了起来。说书人清了清嗓子，把那段尘封多年的往事，慢慢讲给我们听。" },
    { label: "历史纪实", description: "五十岁左右的男性讲述者，嗓音低沉稳重，吐字清晰，语速从容，带有纪录片旁白的厚重感", text: "一九三六年冬天，西安城里寒风凛冽。这场突如其来的事变，彻底改变了此后十年的政治格局。" },
    { label: "现代小说", description: "三十岁左右的女性，嗓音温柔细腻，语气自然，有细腻的情感起伏，像在娓娓讲述一个故事", text: "她推开窗，楼下的桂花开得正盛。十年了，这条巷子几乎没变，只是再也没有人喊她回家吃饭。" },
    { label: "悬疑推理", description: "三十多岁的男性，嗓音低沉略带沙哑，语气克制冷静，营造紧张神秘的氛围", text: "门是从里面反锁的，窗户也钉死了。可就在凌晨三点，走廊尽头那间空屋子，又亮起了灯。" },
    { label: "武侠古风", description: "三十岁左右的男性，嗓音清朗有力，带一点江湖气，节奏抑扬顿挫，富有画面感", text: "长剑出鞘，寒光映雪。少年立于断崖之上，衣袂翻飞，身后是千军万马，身前是万丈深渊。" },
    { label: "科普知识", description: "三十岁左右的男性，嗓音明亮清晰，语气亲切轻松，像一位耐心讲解的老师", text: "我们每天呼吸的空气里，氧气只占五分之一。那么剩下的大部分是什么？答案可能出乎你的意料。" },
    { label: "商业社科", description: "四十岁左右的男性，嗓音沉稳干练，语速适中，条理清晰，带有专业可信的气质", text: "一家企业能够成功，不只是因为抓住了机会，更在于它在无人看好的时候，依然坚持了正确的判断。" },
    { label: "散文随笔", description: "三十岁左右的女性，嗓音柔和温暖，语气安静从容，带一点文艺气息", text: "秋天的午后，阳光斜斜地照进书房。我泡一壶茶，翻开一本旧书，时间好像也跟着慢了下来。" },
    { label: "儿童故事", description: "二十多岁的女性，嗓音甜美活泼，语调生动有起伏，充满童趣和温暖", text: "小兔子抱着胡萝卜，蹦蹦跳跳地回家了。它想，今天一定要把最甜的那一根，留给生病的奶奶。" },
  ];

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    }[character]));
  }

  function showToast(message) {
    const toast = document.getElementById("toast");
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add("visible");
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => toast.classList.remove("visible"), 3200);
  }

  // 点击遮罩或关闭按钮时关闭 dialog；遮罩点击的 target 是 dialog 自身（内容包在 .dialog-body 中）
  function bindDialogDismiss(dialog) {
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog || event.target.closest(".close-dialog")) dialog.close();
    });
  }

  // 统一弹层：原生 dialog 提供遮罩、焦点管理和 Esc 关闭，关闭后从 DOM 移除
  function openDialog(className, html) {
    const dialog = document.createElement("dialog");
    dialog.className = `app-dialog ${className}`;
    dialog.innerHTML = `<div class="dialog-body">${html}</div>`;
    document.body.appendChild(dialog);
    bindDialogDismiss(dialog);
    dialog.addEventListener("close", () => dialog.remove());
    dialog.showModal();
    return dialog;
  }

  function confirmDialog({ title, message, confirmLabel, danger = false }) {
    return new Promise((resolve) => {
      const dialog = openDialog("confirm-dialog", `<h2>${escapeHtml(title)}</h2><p>${escapeHtml(message)}</p><div class="dialog-actions"><button class="button button-secondary close-dialog" type="button">取消</button><button class="button ${danger ? "button-danger" : "button-primary"} confirm-action" type="button">${escapeHtml(confirmLabel)}</button></div>`);
      let confirmed = false;
      dialog.querySelector(".confirm-action").addEventListener("click", () => { confirmed = true; dialog.close(); });
      dialog.addEventListener("close", () => resolve(confirmed));
    });
  }

  async function request(url, options = {}) {
    const { responseType, ...fetchOptions } = options;
    const response = await fetch(url, fetchOptions);
    if (response.status === 401) {
      window.location.href = "/login";
      throw new Error("未登录");
    }
    const contentType = response.headers.get("content-type") || "";
    if (!response.ok) {
      const body = contentType.includes("application/json") ? await response.json() : await response.text();
      throw new Error(body.detail || body || "请求失败");
    }
    if (responseType === "blob") return response.blob();
    const body = contentType.includes("application/json") ? await response.json() : await response.text();
    return body;
  }

  function formatNumber(value) {
    return Number(value || 0).toLocaleString("zh-CN");
  }

  function formatDuration(seconds) {
    const total = Math.max(0, Number(seconds || 0));
    if (total < 60) return `${Math.round(total)} 秒`;
    const minutes = Math.round(total / 60);
    if (minutes < 60) return `约 ${minutes} 分钟`;
    const hours = Math.floor(minutes / 60);
    return `约 ${hours} 小时 ${minutes % 60} 分钟`;
  }

  function bookCover(book, className = "book-cover") {
    if (book.has_cover) {
      return `<img class="${className}" src="/api/books/${book.id}/cover" alt="${escapeHtml(book.title)}的封面" />`;
    }
    return `<div class="${className} book-cover-fallback" aria-hidden="true">${escapeHtml((book.title || "书籍").slice(0, 7))}</div>`;
  }

  function renderBookCard(book) {
    const progress = Number(book.progress || 0);
    const action = `<a class="button ${book.status === "completed" ? "button-secondary" : "button-primary"}" href="/books/${book.id}">${book.status === "completed" ? "查看成品" : "继续制作"}</a>`;
    const detail = book.status === "completed"
      ? `${book.chapter_count || 0} 章已生成章节音频`
      : `${book.chapter_count || 0} 章 · ${formatNumber(book.total_characters)} 字`;
    return `<article class="book-card">
      ${bookCover(book)}
      <div class="book-info"><h3 title="${escapeHtml(book.title)}">${escapeHtml(book.title)}</h3><div class="book-file" title="${escapeHtml(book.source_filename)}">${escapeHtml(book.source_filename)}</div><div class="book-meta">${escapeHtml(book.author || "作者未标注")}</div></div>
      <div class="book-progress"><div class="progress-copy"><strong>${escapeHtml(statusLabels[book.status] || book.status)}</strong><span>${escapeHtml(detail)}</span></div><div class="progress-bar"><span style="width:${progress}%"></span></div></div>
      <div class="book-card-action">${action}</div>
    </article>`;
  }

  function providerById(providerId) {
    return ttsProviders.find((provider) => provider.id === providerId);
  }

  function voiceById(providerId, voiceId) {
    return providerById(providerId)?.voices.find((voice) => voice.id === voiceId);
  }

  function providerOptionsHtml(selectedProvider) {
    return ttsProviders.map((provider) => `<label class="tts-provider-option ${provider.id === selectedProvider ? "selected" : ""}"><input name="tts_provider" type="radio" value="${escapeHtml(provider.id)}" ${provider.id === selectedProvider ? "checked" : ""}/><span><strong>${escapeHtml(provider.name)}</strong><small title="${escapeHtml(provider.model)}">${escapeHtml(provider.model)}</small></span></label>`).join("");
  }

  function voiceOptionsHtml(providerId, selectedVoice) {
    const provider = providerById(providerId);
    if (!provider) return "";
    const cards = provider.voices.map((voice) => `<article class="voice-option ${voice.id === selectedVoice ? "selected" : ""}">
      <label><input name="voice" type="radio" value="${escapeHtml(voice.id)}" ${voice.id === selectedVoice ? "checked" : ""}/><span class="voice-option-copy"><strong>${escapeHtml(voice.name)}</strong><small>${escapeHtml(voice.description)}</small></span></label>
      <span class="voice-option-actions">
        <button class="voice-preview" aria-label="试听 ${escapeHtml(voice.name)} 的音色" data-provider="${escapeHtml(providerId)}" data-voice="${escapeHtml(voice.id)}" data-preview-text="${escapeHtml(voice.preview_text)}" type="button">试听</button>
        ${voice.cloned ? `<button class="voice-delete" aria-label="删除音色 ${escapeHtml(voice.name)}" data-voice="${escapeHtml(voice.id)}" data-voice-name="${escapeHtml(voice.name)}" type="button">删除</button>` : ""}
      </span>
    </article>`).join("");
    const addCard = provider.supports_clone ? `<button class="voice-option voice-add" type="button"><strong>＋ 新建克隆音色</strong><small>用 MiMo 设计声音，或上传一段录音</small></button>` : "";
    return cards + addCard;
  }

  async function refreshTtsProviders() {
    const data = await request("/api/tts/providers");
    ttsProviders = data.providers || [];
    if (!ttsProviders.length) throw new Error("TTS 供应商目录为空");
    return data;
  }

  // 新建克隆音色：可用 MiMo 按描述设计样音，也可上传录音；成功后返回新音色 ID，取消返回 null
  function openCloneVoiceDialog() {
    return new Promise((resolve) => {
      const dialog = openDialog("clone-voice-dialog", `<div class="dialog-header"><h2>新建克隆音色</h2><button class="text-button close-dialog" type="button">关闭</button></div>
        <form class="clone-voice-form">
          <div class="range-mode clone-mode" role="tablist" aria-label="音色来源"><button type="button" class="active" data-mode="design">用 MiMo 设计</button><button type="button" data-mode="upload">上传录音</button></div>
          <label class="field">音色名称<input name="name" type="text" maxlength="40" required placeholder="例如：说书老李" /></label>
          <div class="clone-panel" data-panel="design">
            <label class="field">声音描述<textarea name="description" rows="2" maxlength="500" placeholder="描述想要的声音：年龄、性别、嗓音特点、语气">${escapeHtml(DESIGN_SAMPLE_TEXTS[0].description)}</textarea></label>
            <div class="field">样音文本
              <div class="sample-presets" role="group" aria-label="样音题材">${DESIGN_SAMPLE_TEXTS.map((sample, index) => `<button type="button" class="sample-preset ${index === 0 ? "active" : ""}" data-index="${index}">${escapeHtml(sample.label)}</button>`).join("")}</div>
              <textarea name="design_text" rows="2" maxlength="200" aria-label="样音文本">${escapeHtml(DESIGN_SAMPLE_TEXTS[0].text)}</textarea>
            </div>
            <button class="button button-secondary design-generate" type="button">生成样音</button>
          </div>
          <div class="clone-panel hidden" data-panel="upload">
            <label class="field">参考音频（${CLONE_SECONDS_HINT}，单人、无背景音乐）<input name="file" type="file" accept="audio/*" /></label>
            <label class="field">参考音频里说的原话<textarea name="ref_text" rows="3" placeholder="逐字填写音频中的内容，越准确克隆效果越好"></textarea></label>
          </div>
          <audio class="clone-voice-audio hidden" controls preload="none"></audio>
          <div class="clone-voice-error alert alert-error hidden"></div>
          <button class="button button-primary button-block" type="submit" disabled>保存为音色</button>
        </form>`);
      let createdId = null;
      let mode = "design";
      // 设计出的样音及其对应文本；文本被改动后样音作废，保证参考原话与音频一致
      let designed = null;
      const form = dialog.querySelector(".clone-voice-form");
      const audio = dialog.querySelector(".clone-voice-audio");
      const error = dialog.querySelector(".clone-voice-error");
      const submit = form.querySelector('button[type="submit"]');
      const generate = form.querySelector(".design-generate");

      const showAudio = (blob) => {
        if (audio.src) URL.revokeObjectURL(audio.src);
        audio.removeAttribute("src");
        audio.classList.toggle("hidden", !blob);
        if (blob) audio.src = URL.createObjectURL(blob);
      };
      const referenceBlob = () => (mode === "design" ? designed?.blob : form.elements.file.files[0]) || null;
      const refresh = () => {
        submit.disabled = !referenceBlob();
        showAudio(referenceBlob());
      };
      const showError = (message) => {
        error.textContent = message;
        error.classList.toggle("hidden", !message);
      };

      form.querySelectorAll(".clone-mode button").forEach((button) => button.addEventListener("click", () => {
        mode = button.dataset.mode;
        form.querySelectorAll(".clone-mode button").forEach((item) => item.classList.toggle("active", item === button));
        form.querySelectorAll(".clone-panel").forEach((panel) => panel.classList.toggle("hidden", panel.dataset.panel !== mode));
        showError("");
        refresh();
      }));
      form.elements.file.addEventListener("change", refresh);
      const syncPresetSelection = () => {
        const text = form.elements.design_text.value.trim();
        form.querySelectorAll(".sample-preset").forEach((button) => button.classList.toggle("active", DESIGN_SAMPLE_TEXTS[button.dataset.index].text === text));
      };
      form.elements.design_text.addEventListener("input", () => { designed = null; syncPresetSelection(); refresh(); });
      form.querySelectorAll(".sample-preset").forEach((button) => button.addEventListener("click", () => {
        const sample = DESIGN_SAMPLE_TEXTS[button.dataset.index];
        // 描述为空或仍是某个题材的推荐时才替换，不覆盖用户自己写的描述
        const description = form.elements.description.value.trim();
        if (!description || DESIGN_SAMPLE_TEXTS.some((item) => item.description === description)) {
          form.elements.description.value = sample.description;
        }
        form.elements.design_text.value = sample.text;
        form.elements.design_text.dispatchEvent(new Event("input"));
      }));
      generate.addEventListener("click", async () => {
        const description = form.elements.description.value.trim();
        const text = form.elements.design_text.value.trim();
        if (!description || !text) { showError("请填写声音描述和样音文本"); return; }
        showError("");
        generate.disabled = true;
        generate.textContent = "正在生成…";
        try {
          const blob = await request("/api/tts/voice-design", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ description, text }),
            responseType: "blob",
          });
          designed = { blob, text };
          refresh();
          audio.play().catch(() => {});
        } catch (designError) {
          showError(designError.message);
        } finally {
          generate.disabled = false;
          generate.textContent = designed ? "重新生成" : "生成样音";
        }
      });

      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const name = form.elements.name.value.trim();
        const refText = mode === "design" ? designed?.text : form.elements.ref_text.value.trim();
        if (!name || !refText) { showError(mode === "design" ? "请填写音色名称" : "请填写音色名称和参考音频里说的原话"); return; }
        const body = new FormData();
        body.append("name", name);
        body.append("ref_text", refText);
        body.append("file", referenceBlob(), mode === "design" ? "design.wav" : form.elements.file.files[0].name);
        submit.disabled = true;
        submit.textContent = "正在保存…";
        showError("");
        try {
          const result = await request("/api/tts/clone-voices", { method: "POST", body });
          createdId = result.voice.id;
          dialog.close();
        } catch (createError) {
          showError(createError.message);
          submit.disabled = false;
          submit.textContent = "保存为音色";
        }
      });
      dialog.addEventListener("close", () => {
        showAudio(null);
        resolve(createdId);
      });
    });
  }

  // 绑定克隆音色的新建/删除；变更后刷新目录并通过 onChanged 回传应选中的音色
  function bindCloneVoiceActions(container, errorNode, onChanged) {
    container.querySelector(".voice-add")?.addEventListener("click", async () => {
      const createdId = await openCloneVoiceDialog();
      if (!createdId) return;
      await refreshTtsProviders();
      onChanged(createdId);
    });
    container.querySelectorAll(".voice-delete").forEach((button) => button.addEventListener("click", async () => {
      const confirmed = await confirmDialog({
        title: "删除克隆音色",
        message: `确定删除「${button.dataset.voiceName}」？参考音频会一并删除。`,
        confirmLabel: "删除",
        danger: true,
      });
      if (!confirmed) return;
      errorNode.classList.add("hidden");
      try {
        await request(`/api/tts/clone-voices/${encodeURIComponent(button.dataset.voice)}`, { method: "DELETE" });
        await refreshTtsProviders();
        onChanged(null);
      } catch (deleteError) {
        errorNode.textContent = deleteError.message;
        errorNode.classList.remove("hidden");
      }
    }));
  }

  function updateVoiceSelection(root = document) {
    root.querySelectorAll(".voice-option").forEach((card) => {
      const radio = card.querySelector('input[name="voice"]');
      card.classList.toggle("selected", Boolean(radio?.checked));
    });
  }

  async function previewVoice(provider, voice, previewText, button, audioElement = null, errorElement = null, previewElement = null) {
    const audio = audioElement || document.getElementById("voice-preview-audio");
    const error = errorElement || document.getElementById("voice-error");
    const previewTextNode = previewElement || document.getElementById("voice-preview-text");
    if (!audio || !error) return;
    if (previewTextNode) previewTextNode.textContent = previewText;
    button.disabled = true;
    button.textContent = "生成中…";
    error.classList.add("hidden");
    try {
      const blob = await request("/api/tts/voice-preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider, voice }),
        responseType: "blob",
      });
      const previousUrl = audio.dataset.objectUrl;
      if (previousUrl) URL.revokeObjectURL(previousUrl);
      const objectUrl = URL.createObjectURL(blob);
      audio.dataset.objectUrl = objectUrl;
      audio.src = objectUrl;
      audio.classList.remove("hidden");
      audio.load();
      const playback = audio.play();
      if (playback) playback.catch(() => {});
    } catch (previewError) {
      error.textContent = previewError.message;
      error.classList.remove("hidden");
    } finally {
      button.disabled = false;
      button.textContent = "试听";
    }
  }

  async function loadTtsProviders() {
    if (ttsProviders.length) return ttsProviders;
    await refreshTtsProviders();
    return ttsProviders;
  }

  async function loadVoiceCatalog() {
    const providersNode = document.getElementById("tts-provider-options");
    const options = document.getElementById("voice-options");
    const status = document.getElementById("voice-picker-status");
    const previewText = document.getElementById("voice-preview-text");
    const error = document.getElementById("voice-error");
    const submit = document.getElementById("upload-submit");
    if (!providersNode || !options || !status || !previewText || !error || !submit) return;
    try {
      const data = await refreshTtsProviders();
      const renderProvider = (providerId, selectedVoice = null) => {
        const provider = providerById(providerId);
        if (!provider) throw new Error("TTS 供应商目录为空");
        const voiceId = selectedVoice && voiceById(providerId, selectedVoice) ? selectedVoice : provider.default_voice;
        providersNode.innerHTML = providerOptionsHtml(providerId);
        options.innerHTML = voiceOptionsHtml(providerId, voiceId);
        previewText.textContent = voiceById(providerId, voiceId)?.preview_text || "";
        status.textContent = `${provider.name} · ${provider.voices.length} 个音色`;
        // 没有可选音色（本地模型尚未创建克隆音色）时不能上传
        submit.disabled = !voiceId;
        providersNode.querySelectorAll('input[name="tts_provider"]').forEach((radio) => radio.addEventListener("change", () => renderProvider(radio.value)));
        options.querySelectorAll('input[name="voice"]').forEach((radio) => radio.addEventListener("change", () => {
          updateVoiceSelection(options);
          previewText.textContent = voiceById(providerId, radio.value)?.preview_text || "";
        }));
        options.querySelectorAll(".voice-preview").forEach((button) => button.addEventListener("click", () => previewVoice(button.dataset.provider, button.dataset.voice, button.dataset.previewText, button)));
        bindCloneVoiceActions(options, error, (voiceIdToSelect) => {
          const current = options.querySelector('input[name="voice"]:checked')?.value;
          renderProvider(providerId, voiceIdToSelect || current);
        });
      };
      renderProvider(data.defaults.provider, data.defaults.voice);
    } catch (loadError) {
      status.textContent = "音色加载失败";
      error.textContent = loadError.message;
      error.classList.remove("hidden");
    }
  }

  async function loadBooks() {
    const data = await request("/api/books");
    const active = data.books.filter((book) => book.status !== "completed");
    const completed = data.books.filter((book) => book.status === "completed");
    const activeList = document.getElementById("active-books");
    const completedList = document.getElementById("completed-books");
    if (!activeList || !completedList) return;
    activeList.innerHTML = active.map(renderBookCard).join("");
    completedList.innerHTML = completed.map(renderBookCard).join("");
    document.getElementById("active-empty").classList.toggle("hidden", active.length > 0);
    document.getElementById("completed-empty").classList.toggle("hidden", completed.length > 0);
    document.getElementById("active-count").textContent = `${active.length} 本`;
    document.getElementById("completed-count").textContent = `${completed.length} 本`;
  }

  function setupUpload() {
    const form = document.getElementById("upload-form");
    const styleOptions = document.getElementById("speech-style-options");
    if (styleOptions) {
      styleOptions.innerHTML = speechStyleOptionsHtml("narrated");
      bindSpeechStyleOptions(styleOptions);
    }
    const fileInput = document.getElementById("book-file");
    const choose = document.getElementById("choose-file");
    const selected = document.getElementById("selected-file");
    const error = document.getElementById("upload-error");
    const submit = document.getElementById("upload-submit");
    if (!form || !fileInput) return;
    choose.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => { selected.textContent = fileInput.files[0]?.name || "尚未选择文件"; });
    ["dragenter", "dragover"].forEach((name) => form.addEventListener(name, (event) => { event.preventDefault(); choose.classList.add("dragging"); }));
    ["dragleave", "drop"].forEach((name) => form.addEventListener(name, (event) => { event.preventDefault(); choose.classList.remove("dragging"); }));
    form.addEventListener("drop", (event) => { if (event.dataTransfer.files.length) { fileInput.files = event.dataTransfer.files; fileInput.dispatchEvent(new Event("change")); } });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      error.classList.add("hidden");
      if (!fileInput.files.length) { error.textContent = "请选择 EPUB 或 TXT 文件"; error.classList.remove("hidden"); return; }
      submit.disabled = true;
      submit.textContent = "正在上传…";
      try {
        const result = await request("/api/books", { method: "POST", body: new FormData(form) });
        window.location.href = `/books/${result.book_id}`;
      } catch (uploadError) {
        error.textContent = uploadError.message;
        error.classList.remove("hidden");
        submit.disabled = false;
        submit.textContent = "上传并建立索引";
      }
    });
  }

  function setupDashboard() {
    const uploadDialog = document.getElementById("upload-dialog");
    bindDialogDismiss(uploadDialog);
    document.querySelectorAll("#open-upload, .open-upload-inline").forEach((button) => button.addEventListener("click", () => uploadDialog.showModal()));
    document.getElementById("open-tts-settings").addEventListener("click", async () => {
      try {
        const catalog = await refreshTtsProviders();
        showTtsSettings({ config: { tts_provider: catalog.defaults.provider, voice: catalog.defaults.voice } }, {
          title: "新书默认配音",
          url: "/api/tts/defaults",
          saved: async () => {
            showToast("新书默认配音已保存");
            await loadVoiceCatalog();
          },
        });
      } catch (error) {
        showToast(error.message);
      }
    });
    setupUpload();
    loadVoiceCatalog();
    loadBooks().catch((error) => showToast(error.message));
    window.setInterval(() => loadBooks().catch(() => {}), 4000);
  }

  function selectionPayload() {
    const payload = { selection_mode: workbench.mode };
    if (workbench.mode === "chapters") {
      payload.chapter_start = Number(workbench.chapterStart);
      payload.chapter_end = Number(workbench.chapterEnd);
    } else if (workbench.mode === "characters") {
      payload.target_characters = Number(workbench.targetCharacters);
    } else if (workbench.advancedUnit === "segment") {
      payload.start_segment = Number(workbench.startSegment) || undefined;
      payload.end_segment = Number(workbench.endSegment) || undefined;
    } else if (workbench.advancedUnit === "chapter") {
      payload.chapter_start = Number(workbench.chapterStart) || undefined;
      payload.chapter_end = Number(workbench.chapterEnd) || undefined;
    } else {
      payload.start_batch = Number(workbench.startBatch) || undefined;
      payload.end_batch = Number(workbench.endBatch) || undefined;
    }
    return payload;
  }

  function stagePayload(stage) {
    // 章节范围具有稳定业务含义，始终按章节提交，避免旧分段范围截断整章。
    const payload = workbench.rangeConfirmed && workbench.lockedRange && workbench.mode !== "chapters"
      ? { selection_mode: "advanced", ...workbench.lockedRange }
      : selectionPayload();
    payload.start_stage = stage;
    payload.end_stage = stage;
    payload.output_mode = stage === "package" ? "chapters" : "none";
    return payload;
  }

  function persistWorkbench() {
    if (!bookId) return;
    localStorage.setItem(`book-tts-studio-${bookId}`, JSON.stringify({
      mode: workbench.mode,
      chapterStart: workbench.chapterStart,
      chapterEnd: workbench.chapterEnd,
      targetCharacters: workbench.targetCharacters,
      startBatch: workbench.startBatch,
      endBatch: workbench.endBatch,
      startSegment: workbench.startSegment,
      endSegment: workbench.endSegment,
      advancedUnit: workbench.advancedUnit,
      lockedRange: workbench.lockedRange,
      rangeConfirmed: workbench.rangeConfirmed,
    }));
  }

  function restoreWorkbench() {
    if (!bookId) return;
    const raw = localStorage.getItem(`book-tts-studio-${bookId}`);
    if (!raw) return;
    try {
      const saved = JSON.parse(raw);
      ["mode", "chapterStart", "chapterEnd", "targetCharacters", "startBatch", "endBatch", "startSegment", "endSegment", "advancedUnit", "lockedRange", "rangeConfirmed"].forEach((key) => {
        if (saved[key] !== undefined) workbench[key] = saved[key];
      });
    } catch (error) {
      localStorage.removeItem(`book-tts-studio-${bookId}`);
      console.warn("无法恢复制作范围", error);
    }
  }

  function stagePreview(stage) {
    return workbench.stagePreviews[stage] || {};
  }

  function executionMatchesRange(execution) {
    const locked = workbench.lockedRange;
    if (!locked) return true;
    return Number(execution.resolved_start_sequence) + 1 === Number(locked.start_segment)
      && Number(execution.resolved_end_sequence) + 1 === Number(locked.end_segment);
  }

  function stageExecutions(data, stage) {
    return (data.executions || []).filter((execution) => executionMatchesRange(execution)
      && (execution.current_stage === stage || execution.start_stage === stage || execution.end_stage === stage));
  }

  function stageState(data) {
    if (!workbench.rangeConfirmed) return { current: "range", index: 1 };
    const active = data.active_execution;
    if (active && executionMatchesRange(active)) {
      return { current: active.current_stage || active.start_stage, index: stageOrder.indexOf(active.current_stage || active.start_stage) + 2 };
    }
    const precheck = stagePreview("precheck");
    const synthesize = stagePreview("synthesize");
    const precheckDone = Number(precheck.precheck_count || 0) > 0
      && Number(precheck.precheck_completed_count || 0) >= Number(precheck.precheck_count || 0);
    const audioDone = Number(synthesize.audio_total_count || 0) > 0
      && Number(synthesize.audio_completed_count || 0) >= Number(synthesize.audio_total_count || 0);
    const packageDone = stageExecutions(data, "package").some((execution) => execution.status === "completed");
    if (!precheckDone || !stageExecutions(data, "synthesize").length) return { current: "precheck", index: 2, review: precheckDone };
    if (!audioDone || !stageExecutions(data, "package").length) return { current: "synthesize", index: 3, review: audioDone };
    return { current: "package", index: 4, review: packageDone };
  }

  function renderBookHeader(data) {
    const title = document.getElementById("topbar-book-title");
    title.textContent = data.title || "书稿";
    title.title = `${data.title || "书稿"} · ${statusLabels[data.status] || data.status}`;
  }

  function processStatus(data, number) {
    if (["uploaded", "parsing"].includes(data.status)) return number === 1 ? "正在准备" : "等待书稿准备";
    const state = stageState(data);
    if (number < state.index) return "已完成";
    if (number === state.index) return state.review ? "等待你的确认" : "当前步骤";
    return "等待上一步";
  }

  function renderProcessNav(data) {
    const state = stageState(data);
    const steps = ["确定范围", "检查朗读稿", "生成并试听", "导出章节"];
    const items = steps.map((step, index) => {
      const number = index + 1;
      const active = workbench.view === "workflow" && state.index === number;
      const done = processStatus(data, number) === "已完成";
      const canOpen = number === 1 || number <= state.index;
      return `<button type="button" class="process-step ${active ? "active" : ""} ${done ? "done" : ""}" ${canOpen ? "" : "disabled"}><span class="process-number">${number}</span><span><strong>${step}</strong>${active ? `<small>${processStatus(data, number)}</small>` : ""}</span></button>`;
    }).join("");
    document.getElementById("process-nav").innerHTML = `<h2>制作流程</h2><div class="process-steps">${items}</div><div class="process-secondary"><button type="button" class="secondary-nav ${workbench.view === "chapters" ? "active" : ""}" data-view="chapters">章节内容</button><button type="button" class="secondary-nav ${workbench.view === "history" ? "active" : ""}" data-view="history">处理记录</button></div>`;
  }

  function chapterSelectionText(data) {
    if (workbench.mode === "chapters") {
      const start = data.chapters?.[workbench.chapterStart - 1];
      const end = data.chapters?.[workbench.chapterEnd - 1];
      if (!start) return "尚未选择章节";
      if (workbench.chapterStart === workbench.chapterEnd) return `第 ${workbench.chapterStart} 章 · ${start.title}`;
      return `第 ${workbench.chapterStart} 章至第 ${workbench.chapterEnd} 章`;
    }
    if (workbench.mode === "characters") return `从当前进度开始，目标 ${formatNumber(workbench.targetCharacters)} 字`;
    const preview = workbench.rangePreview;
    return preview ? `全书第 ${preview.resolved_start_sequence}–${preview.resolved_end_sequence} 段` : "精确范围";
  }

  function renderModeSwitch() {
    return `<div class="range-mode" role="tablist" aria-label="范围选择方式"><button type="button" class="scope-mode ${workbench.mode === "chapters" ? "active" : ""}" data-mode="chapters">按章节</button><button type="button" class="scope-mode ${workbench.mode === "characters" ? "active" : ""}" data-mode="characters">按字数</button><button type="button" class="scope-mode ${workbench.mode === "advanced" ? "active" : ""}" data-mode="advanced">精确范围</button></div>`;
  }

  function renderChapterSelector(data) {
    const start = Number(workbench.chapterStart);
    const end = Number(workbench.chapterEnd);
    return `<div class="chapter-select-table ${workbench.chapterPendingStart ? "picking-end" : ""}"><div class="chapter-select-head"><span>章节</span><span>章节名称</span><span>字数</span></div>${(data.chapters || []).map((chapter) => {
      const number = Number(chapter.chapter_index) + 1;
      const selected = number >= start && number <= end;
      const edges = `${number === start ? "range-start" : ""} ${number === end ? "range-end" : ""}`;
      return `<button type="button" class="chapter-select-row ${selected ? `selected ${edges}` : ""}" data-chapter-number="${number}" aria-pressed="${selected}"><span>第 ${number} 章</span><strong title="${escapeHtml(chapter.title)}">${escapeHtml(chapter.title)}</strong><span>${formatNumber(chapter.char_count)}</span></button>`;
    }).join("")}</div>`;
  }

  function renderRangeControls(data) {
    if (workbench.mode === "chapters") return renderChapterSelector(data);
    if (workbench.mode === "characters") {
      return `<div class="simple-range-control"><label>这次大约处理多少字<input class="scope-control" data-key="targetCharacters" type="number" min="1" max="10000000" step="500" value="${workbench.targetCharacters}" /></label><p>系统会从当前未完成的位置开始，并自动保持朗读分段完整。</p></div>`;
    }
    return `<div class="precise-range"><div class="advanced-unit-switch"><button type="button" class="advanced-unit ${workbench.advancedUnit === "batch" ? "active" : ""}" data-unit="batch">预检批次</button><button type="button" class="advanced-unit ${workbench.advancedUnit === "segment" ? "active" : ""}" data-unit="segment">全书分段</button><button type="button" class="advanced-unit ${workbench.advancedUnit === "chapter" ? "active" : ""}" data-unit="chapter">章节编号</button></div><div class="precise-fields">${workbench.advancedUnit === "batch" ? `<label>从第几批<input class="scope-control" data-key="startBatch" type="number" min="1" max="${data.precheck_count || 1}" value="${workbench.startBatch}" /></label><label>到第几批<input class="scope-control" data-key="endBatch" type="number" min="1" max="${data.precheck_count || 1}" value="${workbench.endBatch}" /></label>` : workbench.advancedUnit === "segment" ? `<label>从第几段<input class="scope-control" data-key="startSegment" type="number" min="1" max="${data.total_segments || 1}" value="${workbench.startSegment}" /></label><label>到第几段<input class="scope-control" data-key="endSegment" type="number" min="1" max="${data.total_segments || 1}" value="${workbench.endSegment}" /></label>` : `<label>从第几章<input class="scope-control" data-key="chapterStart" type="number" min="1" max="${data.chapter_count || 1}" value="${workbench.chapterStart}" /></label><label>到第几章<input class="scope-control" data-key="chapterEnd" type="number" min="1" max="${data.chapter_count || 1}" value="${workbench.chapterEnd}" /></label>`}</div><p>精确范围适合复现或重试已有任务；读音检查会对齐完整批次，生成语音按所选分段执行。</p></div>`;
  }

  function renderRangePanel(data) {
    const node = document.getElementById("range-panel");
    if (workbench.view !== "workflow" || workbench.rangeConfirmed) { node.innerHTML = ""; return; }
    if (["uploaded", "parsing"].includes(data.status)) {
      node.innerHTML = `<section class="workspace-panel preparing-state"><h2>正在建立书稿索引</h2><p>这一步只在本地识别章节和朗读分段，不会调用云端服务。</p><div class="preparing-line"><span></span></div></section>`;
      return;
    }
    node.innerHTML = `<section class="workspace-panel range-workspace"><header class="workspace-heading range-heading"><h2>选择这次要处理的内容</h2><button class="text-button reparse-book" type="button">重新识别正文</button></header>${renderModeSwitch()}${renderRangeControls(data)}${workbench.previewError ? `<div class="inline-message error">${escapeHtml(workbench.previewError)}</div>` : ""}</section>`;
  }

  const SPEECH_STYLES = [
    { id: "narrated", name: "讲述改写", description: "像主讲人讲给你听，短句、口语，更生动" },
    { id: "faithful", name: "贴近原文", description: "只清理注释和符号，保留原文的表达" },
  ];

  function speechStyleOptionsHtml(selected) {
    return SPEECH_STYLES.map((style) => `<label class="tts-provider-option ${style.id === selected ? "selected" : ""}"><input name="speech_style" type="radio" value="${style.id}" ${style.id === selected ? "checked" : ""}/><span><strong>${style.name}</strong><small>${style.description}</small></span></label>`).join("");
  }

  function bindSpeechStyleOptions(container) {
    container.querySelectorAll('input[name="speech_style"]').forEach((radio) => radio.addEventListener("change", () => {
      container.querySelectorAll(".tts-provider-option").forEach((card) => card.classList.toggle("selected", card.querySelector("input").checked));
    }));
  }

  function renderSpeechStyleCard(data) {
    const style = SPEECH_STYLES.find((item) => item.id === data.config?.speech_style);
    return `<section class="tts-config-card"><span>朗读稿风格</span><strong>${escapeHtml(style?.name || "未配置")}</strong><small>${escapeHtml(style?.description || "")}</small><button id="edit-speech-style" class="text-button" type="button">修改朗读稿风格</button></section>`;
  }

  function showSpeechStyleSettings(data) {
    const current = data.config?.speech_style;
    const dialog = openDialog("speech-style-dialog", `<div class="dialog-header"><h2>朗读稿风格</h2><button class="text-button close-dialog" type="button">关闭</button></div><form class="speech-style-form"><div class="tts-provider-options">${speechStyleOptionsHtml(current)}</div><p class="muted-text">修改后对之后的读音检查生效；已检查的章节需要「重新检查本章」才会换成新风格。</p><button class="button button-primary button-block" type="submit">保存</button></form>`);
    const form = dialog.querySelector(".speech-style-form");
    bindSpeechStyleOptions(form);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const selected = form.querySelector('input[name="speech_style"]:checked')?.value;
      if (selected === current) { dialog.close(); return; }
      try {
        await request(`/api/books/${bookId}/speech-style`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ speech_style: selected }) });
        dialog.close();
        showToast("朗读稿风格已保存");
        await loadDetail();
      } catch (error) {
        showToast(error.message);
      }
    });
  }

  function renderTtsConfigCard(data) {
    const providerId = data.config?.tts_provider;
    const voiceId = data.config?.voice;
    const provider = providerById(providerId);
    const voiceName = voiceById(providerId, voiceId)?.name || (voiceId ? "音色不可用，请重新选择" : "未配置音色");
    return `<section class="tts-config-card"><span>当前配音</span><strong>${escapeHtml(provider?.name || providerId || "未配置")}</strong><small>${escapeHtml(voiceName)}${provider?.model ? ` · ${escapeHtml(provider.model)}` : ""}</small><button id="edit-tts-config" class="text-button" type="button">修改配音设置</button></section>`;
  }

  function renderScopeSummary(data) {
    const node = document.getElementById("execution-summary");
    if (["uploaded", "parsing"].includes(data.status)) {
      node.innerHTML = `<h2>书稿准备中</h2><p>解析完成后，这里会显示你选中的内容。</p>`;
      return;
    }
    const preview = workbench.rangePreview || {};
    const ready = Boolean(preview.actual_char_count) && !workbench.previewError;
    const action = workbench.rangeConfirmed
      ? `<button id="edit-range" class="button button-secondary button-block" type="button">重新选择范围</button>`
      : `<button id="confirm-range" class="button button-primary button-block" type="button" ${ready ? "" : "disabled"}>锁定范围并继续</button>`;
    const resolvedRange = preview.resolved_start_sequence && preview.resolved_end_sequence
      ? `全书第 ${preview.resolved_start_sequence}–${preview.resolved_end_sequence} 段`
      : "正在计算实际范围…";
    const metric = (label, value) => `<div><dt>${label}</dt><dd>${value}</dd></div>`;
    node.innerHTML = `<h2>${workbench.rangeConfirmed ? "本次处理范围" : "已选范围"}</h2><div class="scope-title">${escapeHtml(chapterSelectionText(data))}</div><p class="scope-resolved">${escapeHtml(resolvedRange)}</p><dl class="scope-metrics">${metric("字数", formatNumber(preview.actual_char_count))}${metric("预计时长", escapeHtml(formatDuration(preview.estimated_duration_seconds)))}${metric("章节", preview.chapter_count || 0)}${metric("分段", preview.segment_count || 0)}</dl>${preview.warning_message ? `<p class="scope-warning">${escapeHtml(preview.warning_message)}</p>` : ""}${action}${renderTtsConfigCard(data)}${renderSpeechStyleCard(data)}`;
  }

  function stageProgress(data, stage) {
    const preview = stagePreview(stage);
    if (stage === "precheck") return `${preview.precheck_completed_count || 0} / ${preview.precheck_count || 0} 个检查批次`;
    if (stage === "synthesize") return `${preview.audio_completed_count || 0} / ${preview.audio_total_count || 0} 个语音分段`;
    return `${preview.complete_chapter_count || 0} 个完整章节可导出`;
  }

  function failedExecution(data, stage) {
    return stageExecutions(data, stage).find((execution) => execution.status === "failed");
  }

  function renderStageChapterRows(data, stage) {
    let chapters = data.chapters || [];
    if (workbench.mode === "chapters") chapters = chapters.slice(workbench.chapterStart - 1, workbench.chapterEnd);
    else chapters = chapters.filter((chapter) => chapter.prechecked_segments || chapter.audio_completed || chapter.failed_segments).slice(0, 8);
    if (!chapters.length) return `<div class="empty-state">当前范围还没有可查看的章节结果。</div>`;
    return `<div class="result-list">${chapters.map((chapter) => {
      const progress = stage === "precheck" ? `朗读稿 ${chapter.prechecked_segments}/${chapter.segment_count}` : stage === "synthesize" ? `语音 ${chapter.audio_completed}/${chapter.segment_count}` : chapter.output?.status === "completed" ? "章节文件已生成" : "等待导出";
      return `<button type="button" class="result-row chapter-index-row" data-chapter-id="${chapter.id}"><span><strong>第 ${chapter.chapter_index + 1} 章 · ${escapeHtml(chapter.title)}</strong><small>${formatNumber(chapter.char_count)} 字</small></span><span>${escapeHtml(progress)}</span></button>`;
    }).join("")}</div>`;
  }

  function renderStageWorkflow(data) {
    const node = document.getElementById("stage-workflow");
    if (workbench.view !== "workflow" || !workbench.rangeConfirmed || ["uploaded", "parsing"].includes(data.status)) { node.innerHTML = ""; return; }
    const state = stageState(data);
    const stage = state.current;
    const preview = stagePreview(stage);
    const active = data.active_execution && executionMatchesRange(data.active_execution) && (data.active_execution.current_stage || data.active_execution.start_stage) === stage ? data.active_execution : null;
    const failed = failedExecution(data, stage);
    let status = "可以开始";
    let action = "";
    let message = "";
    if (active) {
      status = executionStatusLabels[active.status] || "执行中";
      const progress = active.progress || 0;
      message = `<div class="active-progress"><div><span style="width:${progress}%"></span></div><p>${active.current_item || 0}/${active.current_total || 0} · ${progress}%</p></div>`;
      action = `<button class="button button-secondary pause-execution" data-execution-id="${active.id}" type="button">暂停当前步骤</button>`;
    } else if (state.review) {
      status = stage === "package" ? "已完成" : "等待你的确认";
      message = `<div class="verification-banner"><strong>${stage === "precheck" ? "朗读稿和发音提示已生成" : stage === "synthesize" ? "这部分语音已经生成" : "章节文件已经导出"}</strong><span>${stage === "package" ? "可以从章节内容中下载文件。" : "请先查看下面的结果，确认没有问题后再继续。"}</span></div>`;
      if (stage === "precheck") action = `<button class="button button-secondary view-chapters" type="button">查看朗读稿</button><button class="button button-primary start-stage" data-stage="synthesize" type="button">确认结果，开始生成语音</button>`;
      if (stage === "synthesize") action = `<button class="button button-secondary view-chapters" type="button">试听分段语音</button><button class="button button-primary start-stage" data-stage="package" type="button" ${preview.can_start ? "" : "disabled"}>确认语音，导出章节文件</button>`;
      if (stage === "package") action = `<button class="button button-primary view-chapters" type="button">查看章节成品</button>`;
    } else if (failed) {
      status = "需要处理";
      message = `<div class="inline-message error"><strong>这一步没有完成</strong><span>${escapeHtml(failed.error_message || "执行失败")}</span></div>`;
      action = `<button class="button button-secondary view-problem" data-execution-id="${failed.id}" type="button">查看完整详情</button><button class="button button-primary retry-execution" data-execution-id="${failed.id}" type="button">重试这一步</button>`;
    } else {
      const labels = {
        precheck: ["生成这部分内容的朗读稿和发音提示。完成后，系统会停下来让你逐段查看。", "开始检查朗读稿"],
        synthesize: ["使用你确认过的朗读稿生成语音。完成后可以逐段试听。", "开始生成语音"],
        package: ["把已经完成的语音整理成章节文件，供下载或放入有声书库。", "导出章节文件"],
      };
      message = `<p class="stage-explanation">${labels[stage][0]}</p>`;
      action = `<button class="button button-primary start-stage" data-stage="${stage}" type="button" ${preview.can_start === false ? "disabled" : ""}>${labels[stage][1]}</button>`;
    }
    node.innerHTML = `<section class="workspace-panel stage-workspace"><header class="stage-heading"><div><span>第 ${state.index} 步</span><h2>${stageLabels[stage]}</h2></div><strong class="stage-status">${status}</strong></header>${message}<div class="stage-result-heading"><h3>${state.review ? "请检查本次结果" : "本次内容"}</h3><span>${escapeHtml(stageProgress(data, stage))}</span></div>${renderStageChapterRows(data, stage)}${preview.prerequisite_gaps?.length ? `<div class="inline-message error">${escapeHtml(preview.prerequisite_gaps.join("；"))}</div>` : ""}<footer class="stage-actions">${action}</footer></section>`;
  }

  function renderChapterBrowser(data) {
    const node = document.getElementById("chapter-browser");
    if (workbench.view !== "chapters") { node.innerHTML = ""; return; }
    const outputs = new Map((data.chapter_outputs || []).map((output) => [output.chapter_id, output]));
    const available = new Set((data.chapter_outputs || []).filter((output) => output.status === "completed" && output.has_file).map((output) => output.chapter_id));
    // 服务推送会重绘列表，保留仍可下载的勾选，清除已失效的成品。
    for (const id of workbench.downloadChapterIds) {
      if (!available.has(id)) workbench.downloadChapterIds.delete(id);
    }
    node.innerHTML = `<section class="workspace-panel browser-workspace"><header class="workspace-heading browser-heading"><h2>章节内容与成品</h2><button type="button" class="text-button return-workflow">返回当前步骤</button></header><form class="chapter-download-form" method="post" action="/api/books/${bookId}/download"><div class="chapter-download-toolbar"><label class="chapter-download-all"><input type="checkbox" id="select-all-downloads" ${available.size ? "" : "disabled"}/>全选成品（${available.size} 章）</label><button type="submit" class="button button-primary" id="download-selected-chapters">下载所选章节（ZIP）</button></div><div class="chapter-browser-list">${(data.chapters || []).map((chapter) => {
      const output = outputs.get(chapter.id);
      const outputAction = output?.status === "completed" && output.has_file ? `<a class="button button-secondary" href="/api/chapter-outputs/${output.id}/download">下载成品</a>` : "";
      return `<article class="chapter-browser-row"><input class="chapter-download-select" type="checkbox" name="chapter_ids" value="${chapter.id}" aria-label="选择第 ${chapter.chapter_index + 1} 章成品" ${available.has(chapter.id) ? "" : "disabled"} ${workbench.downloadChapterIds.has(chapter.id) ? "checked" : ""}/><button type="button" class="chapter-open chapter-index-row" data-chapter-id="${chapter.id}"><span><strong>第 ${chapter.chapter_index + 1} 章 · ${escapeHtml(chapter.title)}</strong><small>${formatNumber(chapter.char_count)} 字 · ${chapter.segment_count} 个分段</small></span><span>朗读稿 ${chapter.prechecked_segments}/${chapter.segment_count}<br/>语音 ${chapter.audio_completed}/${chapter.segment_count}${chapter.taken_over_segments ? `<br/><span class="takeover-badge">本地接管 ${chapter.taken_over_segments} 段</span>` : ""}</span></button>${outputAction}</article>`;
    }).join("")}</div></form></section>`;
    const selectAll = node.querySelector("#select-all-downloads");
    const downloadButton = node.querySelector("#download-selected-chapters");
    const updateSelection = () => {
      const count = workbench.downloadChapterIds.size;
      selectAll.checked = available.size > 0 && count === available.size;
      selectAll.indeterminate = count > 0 && count < available.size;
      downloadButton.disabled = count === 0;
      downloadButton.textContent = count ? `下载所选 ${count} 章（ZIP）` : "下载所选章节（ZIP）";
    };
    node.querySelectorAll(".chapter-download-select").forEach((checkbox) => checkbox.addEventListener("change", () => {
      const id = Number(checkbox.value);
      if (checkbox.checked) workbench.downloadChapterIds.add(id);
      else workbench.downloadChapterIds.delete(id);
      updateSelection();
    }));
    selectAll.addEventListener("change", () => {
      workbench.downloadChapterIds = selectAll.checked ? new Set(available) : new Set();
      node.querySelectorAll(".chapter-download-select").forEach((checkbox) => { checkbox.checked = workbench.downloadChapterIds.has(Number(checkbox.value)); });
      updateSelection();
    });
    updateSelection();
  }

  function executionRange(execution) {
    return `第 ${execution.resolved_start_sequence + 1}–${execution.resolved_end_sequence + 1} 段 · ${formatNumber(execution.actual_char_count)} 字`;
  }

  function renderHistory(data) {
    const executions = data.executions || [];
    if (!executions.length) return `<div class="empty-state">还没有处理记录。</div>`;
    return executions.map((execution) => {
      const actions = [];
      if (["queued", "running"].includes(execution.status)) actions.push(`<button class="button button-secondary pause-execution" data-execution-id="${execution.id}">暂停</button>`);
      if (["failed", "paused"].includes(execution.status)) actions.push(`<button class="button button-secondary retry-execution" data-execution-id="${execution.id}">重试</button>`);
      if (execution.status === "failed") actions.push(`<button class="button button-quiet view-problem" data-execution-id="${execution.id}">查看完整详情</button>`);
      if (execution.has_sample) actions.push(`<a class="button button-secondary" href="/api/executions/${execution.id}/download">下载样例</a>`);
      const stage = execution.current_stage || execution.end_stage;
      return `<article class="history-row ${execution.status}"><div><strong>${escapeHtml(stageLabels[stage] || "处理任务")}</strong><span>${escapeHtml(executionRange(execution))}</span></div><div><strong>${escapeHtml(executionStatusLabels[execution.status] || execution.status)}</strong><span>${execution.warning_message ? escapeHtml(execution.warning_message) : `检查 ${execution.precheck_count} 次 · 语音 ${execution.segment_count} 段`}</span></div><div class="history-actions">${actions.join("")}</div></article>`;
    }).join("");
  }

  function renderHistoryBrowser(data) {
    const node = document.getElementById("history-browser");
    if (workbench.view !== "history") { node.innerHTML = ""; return; }
    node.innerHTML = `<section class="workspace-panel browser-workspace"><header class="workspace-heading browser-heading"><h2>处理记录</h2><button type="button" class="text-button return-workflow">返回当前步骤</button></header><div class="history-list">${renderHistory(data)}</div></section>`;
  }

  // 最近一次执行未结束（排队、执行中、失败或暂停）时返回它；若当前步骤页已经展示了它则不重复提示
  function pendingExecution(data) {
    const latest = (data.executions || [])[0];
    if (!latest || !["queued", "running", "failed", "paused"].includes(latest.status)) return null;
    const shownInWorkflow = workbench.view === "workflow" && workbench.rangeConfirmed && executionMatchesRange(latest);
    return shownInWorkflow ? null : latest;
  }

  function renderPendingExecution(data) {
    const node = document.getElementById("pending-execution");
    const execution = ["uploaded", "parsing"].includes(data.status) ? null : pendingExecution(data);
    if (!execution) { node.innerHTML = ""; return; }
    const stage = execution.current_stage || execution.end_stage;
    const failed = execution.status === "failed";
    node.innerHTML = `<div class="pending-banner ${failed ? "is-failed" : ""}"><div><strong>${escapeHtml(stageLabels[stage] || "处理任务")} · ${escapeHtml(executionStatusLabels[execution.status])}</strong><span>${escapeHtml(executionRange(execution))}</span></div><div class="pending-actions">${failed ? `<button class="button button-quiet view-problem" data-execution-id="${execution.id}" type="button">查看详情</button>` : ""}<button class="button button-secondary open-execution-range" data-execution-id="${execution.id}" type="button">回到这个范围</button></div></div>`;
  }

  // 把工作台切换并锁定到某次执行的范围：章节范围按章节恢复，其他情况按全书分段恢复
  function openExecutionRange(data, execution) {
    const selection = execution.selection || {};
    if (selection.selection_mode === "chapters" && selection.chapter_start && selection.chapter_end) {
      workbench.mode = "chapters";
      workbench.chapterStart = Number(selection.chapter_start);
      workbench.chapterEnd = Number(selection.chapter_end);
    } else {
      workbench.mode = "advanced";
      workbench.advancedUnit = "segment";
      workbench.startSegment = execution.resolved_start_sequence + 1;
      workbench.endSegment = execution.resolved_end_sequence + 1;
    }
    workbench.chapterPendingStart = null;
    workbench.lockedRange = { start_segment: execution.resolved_start_sequence + 1, end_segment: execution.resolved_end_sequence + 1 };
    workbench.rangeConfirmed = true;
    workbench.view = "workflow";
    persistWorkbench();
    queuePreview(data);
  }

  function renderAll(data) {
    workbench.latestData = data;
    renderBookHeader(data);
    renderProcessNav(data);
    renderPendingExecution(data);
    renderRangePanel(data);
    renderStageWorkflow(data);
    renderChapterBrowser(data);
    renderHistoryBrowser(data);
    renderScopeSummary(data);
    document.getElementById("book-error").classList.add("hidden");
    bindWorkspaceControls(data);
  }

  async function refreshPreviews(data) {
    if (["uploaded", "parsing"].includes(data.status)) return;
    const requestId = ++workbench.previewRequestId;
    const basePayload = stagePayload("precheck");
    const payloadForStage = (stage) => ({
      ...basePayload,
      start_stage: stage,
      end_stage: stage,
      output_mode: stage === "package" ? "chapters" : "none",
    });
    try {
      const precheckPreview = await request(`/api/books/${bookId}/execution-preview`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payloadForStage("precheck")) });
      const [synthesizePreview, packagePreview] = await Promise.all([
        request(`/api/books/${bookId}/execution-preview`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payloadForStage("synthesize")) }),
        request(`/api/books/${bookId}/execution-preview`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payloadForStage("package")) }),
      ]);
      if (requestId !== workbench.previewRequestId) return;
      if (workbench.rangeConfirmed) {
        workbench.lockedRange = {
          start_segment: precheckPreview.resolved_start_sequence,
          end_segment: precheckPreview.resolved_end_sequence,
        };
        persistWorkbench();
      }
      workbench.rangePreview = precheckPreview;
      workbench.stagePreviews = { precheck: precheckPreview, synthesize: synthesizePreview, package: packagePreview };
      workbench.previewError = "";
      renderAll(data);
    } catch (error) {
      if (requestId !== workbench.previewRequestId) return;
      workbench.rangePreview = null;
      workbench.stagePreviews = {};
      workbench.previewError = error.message;
      renderAll(data);
    }
  }

  function queuePreview(data) {
    if (["uploaded", "parsing"].includes(data.status)) return;
    workbench.previewRequestId += 1;
    window.clearTimeout(workbench.timer);
    workbench.rangePreview = null;
    workbench.stagePreviews = {};
    renderAll(data);
    workbench.timer = window.setTimeout(() => refreshPreviews(data), 180);
  }

  function updateStateFromControl(target) {
    const key = target.dataset.key;
    if (key) workbench[key] = Number(target.value);
  }

  // 章节范围像日期区间一样选择：第一次点击定起点（先选中这一章），第二次点击定终点；Shift+点击从当前起点直接扩展
  function pickChapter(number, extend) {
    const anchor = workbench.chapterPendingStart || (extend ? Number(workbench.chapterStart) : null);
    if (anchor) {
      workbench.chapterStart = Math.min(anchor, number);
      workbench.chapterEnd = Math.max(anchor, number);
      workbench.chapterPendingStart = null;
      return;
    }
    workbench.chapterStart = number;
    workbench.chapterEnd = number;
    workbench.chapterPendingStart = number;
  }

  // 等待选择终点时，鼠标悬停预览将要选中的区间
  function previewChapterRange(table, hoverNumber) {
    const anchor = workbench.chapterPendingStart;
    table.querySelectorAll(".chapter-select-row").forEach((row) => {
      const number = Number(row.dataset.chapterNumber);
      const inRange = hoverNumber !== null && number >= Math.min(anchor, hoverNumber) && number <= Math.max(anchor, hoverNumber);
      row.classList.toggle("previewing", inRange);
    });
  }

  // 首页默认配音与书籍配音共用同一选择、试听和音色管理弹层。
  function showTtsSettings(data, options = {
    title: "配音设置",
    url: `/api/books/${bookId}/tts-config`,
    saved: async () => {
      workbench.rangePreview = null;
      workbench.stagePreviews = {};
      showToast("配音设置已保存，之后生成的分段会使用新音色");
      await loadDetail();
    },
  }) {
    const currentProvider = data.config?.tts_provider;
    const currentVoice = data.config?.voice;
    if (!providerById(currentProvider)) {
      showToast("当前配音供应商不在可用目录中");
      return;
    }
    const state = { provider: currentProvider, voice: currentVoice };
    const dialog = openDialog("tts-settings-dialog", `<div class="dialog-header"><h2>${escapeHtml(options.title)}</h2><button class="text-button close-dialog" type="button">关闭</button></div><form id="tts-settings-form"><div id="settings-provider-options" class="tts-provider-options"></div><div id="settings-voice-options" class="voice-options"></div><div class="voice-preview-bar"><span id="settings-preview-text" class="muted-text"></span><audio id="settings-preview-audio" class="voice-preview-audio hidden" controls preload="none">你的浏览器不支持音频播放。</audio></div><div id="settings-voice-error" class="alert alert-error hidden"></div><button class="button button-primary button-block" type="submit">保存配音设置</button></form>`);

    const renderSettings = () => {
      const provider = providerById(state.provider);
      if (!provider) return;
      if (!voiceById(state.provider, state.voice)) state.voice = provider.default_voice;
      const providersNode = dialog.querySelector("#settings-provider-options");
      const voicesNode = dialog.querySelector("#settings-voice-options");
      providersNode.innerHTML = providerOptionsHtml(state.provider);
      voicesNode.innerHTML = voiceOptionsHtml(state.provider, state.voice);
      dialog.querySelector("#settings-preview-text").textContent = voiceById(state.provider, state.voice)?.preview_text || "";
      dialog.querySelector('#tts-settings-form button[type="submit"]').disabled = !state.voice;
      bindCloneVoiceActions(voicesNode, dialog.querySelector("#settings-voice-error"), (voiceIdToSelect) => {
        if (voiceIdToSelect) state.voice = voiceIdToSelect;
        renderSettings();
      });
      providersNode.querySelectorAll('input[name="tts_provider"]').forEach((radio) => radio.addEventListener("change", () => {
        state.provider = radio.value;
        state.voice = providerById(state.provider)?.default_voice;
        renderSettings();
      }));
      voicesNode.querySelectorAll('input[name="voice"]').forEach((radio) => radio.addEventListener("change", () => {
        state.voice = radio.value;
        updateVoiceSelection(voicesNode);
        dialog.querySelector("#settings-preview-text").textContent = voiceById(state.provider, state.voice)?.preview_text || "";
      }));
      voicesNode.querySelectorAll(".voice-preview").forEach((button) => button.addEventListener("click", () => previewVoice(
        button.dataset.provider,
        button.dataset.voice,
        button.dataset.previewText,
        button,
        dialog.querySelector("#settings-preview-audio"),
        dialog.querySelector("#settings-voice-error"),
        dialog.querySelector("#settings-preview-text"),
      )));
    };

    renderSettings();
    dialog.querySelector("#tts-settings-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const submit = event.currentTarget.querySelector('button[type="submit"]');
      if (state.provider === currentProvider && state.voice === currentVoice) {
        dialog.close();
        return;
      }
      submit.disabled = true;
      try {
        await request(options.url, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ provider: state.provider, voice: state.voice }),
        });
        dialog.close();
        await options.saved();
      } catch (error) {
        showToast(error.message);
        submit.disabled = false;
      }
    });
  }

  function bindWorkspaceControls(data) {
    document.querySelectorAll(".scope-mode").forEach((button) => button.addEventListener("click", () => {
      workbench.mode = button.dataset.mode;
      workbench.chapterPendingStart = null;
      workbench.lockedRange = null;
      persistWorkbench();
      queuePreview(data);
    }));
    document.querySelectorAll(".advanced-unit").forEach((button) => button.addEventListener("click", () => {
      workbench.advancedUnit = button.dataset.unit;
      persistWorkbench();
      queuePreview(data);
    }));
    document.querySelectorAll(".scope-control").forEach((control) => control.addEventListener("change", () => {
      updateStateFromControl(control);
      persistWorkbench();
      queuePreview(data);
    }));
    document.querySelectorAll(".chapter-select-row").forEach((row) => row.addEventListener("click", (event) => {
      pickChapter(Number(row.dataset.chapterNumber), event.shiftKey);
      persistWorkbench();
      queuePreview(data);
    }));
    const pickingTable = document.querySelector(".chapter-select-table.picking-end");
    if (pickingTable) {
      pickingTable.addEventListener("mouseover", (event) => {
        const row = event.target.closest(".chapter-select-row");
        if (row) previewChapterRange(pickingTable, Number(row.dataset.chapterNumber));
      });
      pickingTable.addEventListener("mouseleave", () => previewChapterRange(pickingTable, null));
    }
    document.querySelector(".reparse-book")?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      const confirmed = await confirmDialog({
        title: "重新识别正文",
        message: "会清除这本书现有的朗读稿、语音进度和处理记录，并按新的正文规则重新建立分段。原始书稿会保留。",
        confirmLabel: "清除并重新识别",
        danger: true,
      });
      if (!confirmed) return;
      button.disabled = true;
      try {
        await request(`/api/books/${bookId}/reparse`, { method: "POST" });
        localStorage.removeItem(`book-tts-studio-${bookId}`);
        window.location.reload();
      } catch (error) {
        showToast(error.message);
        button.disabled = false;
      }
    });
    document.getElementById("confirm-range")?.addEventListener("click", () => {
      if (!workbench.rangePreview) return;
      workbench.chapterPendingStart = null;
      workbench.lockedRange = {
        start_segment: workbench.rangePreview.resolved_start_sequence,
        end_segment: workbench.rangePreview.resolved_end_sequence,
      };
      workbench.rangeConfirmed = true;
      workbench.view = "workflow";
      persistWorkbench();
      refreshPreviews(data);
    });
    document.getElementById("edit-range")?.addEventListener("click", () => {
      workbench.rangeConfirmed = false;
      workbench.lockedRange = null;
      workbench.view = "workflow";
      persistWorkbench();
      queuePreview(data);
    });
    document.getElementById("edit-tts-config")?.addEventListener("click", () => showTtsSettings(data));
    document.getElementById("edit-speech-style")?.addEventListener("click", () => showSpeechStyleSettings(data));
    document.querySelectorAll(".start-stage").forEach((button) => button.addEventListener("click", () => startStageExecution(data, button.dataset.stage)));
    document.querySelectorAll(".view-chapters").forEach((button) => button.addEventListener("click", () => { workbench.view = "chapters"; renderAll(data); }));
    document.querySelectorAll(".return-workflow").forEach((button) => button.addEventListener("click", () => { workbench.view = "workflow"; renderAll(data); }));
    document.querySelectorAll(".secondary-nav").forEach((button) => button.addEventListener("click", () => { workbench.view = button.dataset.view; renderAll(data); }));
    // 步骤导航只负责回到当前步骤；解除已锁定范围只能通过右栏"重新选择范围"
    document.querySelectorAll(".process-step").forEach((button) => button.addEventListener("click", () => {
      workbench.view = "workflow";
      renderAll(data);
    }));
  }

  async function startStageExecution(data, stage) {
    const preview = stagePreview(stage);
    if (!preview.can_start) {
      showToast(preview.prerequisite_gaps?.join("；") || "当前步骤还不能开始");
      return;
    }
    try {
      await request(`/api/books/${bookId}/executions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(stagePayload(stage)) });
      showToast(`${stageLabels[stage]}已开始，完成后会停下来等待你确认`);
      workbench.rangePreview = null;
      workbench.stagePreviews = {};
      await loadDetail();
      openEvents();
    } catch (error) {
      showToast(error.message);
    }
  }

  async function loadDetail() {
    const data = await request(`/api/books/${bookId}`);
    if (data.chapters?.length) {
      workbench.chapterStart = Math.min(Math.max(1, workbench.chapterStart), data.chapters.length);
      workbench.chapterEnd = Math.min(Math.max(workbench.chapterEnd, workbench.chapterStart), data.chapters.length);
      workbench.endBatch = Math.min(Math.max(workbench.endBatch, workbench.startBatch), data.precheck_count || 1);
      workbench.endSegment = Math.min(Math.max(workbench.endSegment, workbench.startSegment), data.total_segments || 1);
    }
    renderAll(data);
    if (!workbench.rangePreview && !["uploaded", "parsing"].includes(data.status)) queuePreview(data);
    return data;
  }

  let events;
  function openEvents() {
    if (events) events.close();
    events = new EventSource(`/api/books/${bookId}/events`);
    events.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        renderAll(data);
        if (!data.active_execution && !["uploaded", "parsing", "executing"].includes(data.status)) {
          events.close();
          refreshPreviews(data);
        }
      } catch (error) {
        console.error(error);
      }
    };
    events.onerror = () => { if (events.readyState === EventSource.CLOSED) return; };
  }

  async function showChapterSegments(chapterId) {
    try {
      const data = await request(`/api/chapters/${chapterId}/segments`);
      openDialog("drawer-dialog", `<div class="dialog-header"><div><span class="dialog-kicker">第 ${data.chapter.chapter_index + 1} 章</span><h2>${escapeHtml(data.chapter.title)}</h2></div><div class="dialog-header-actions"><button class="button button-secondary recheck-chapter" data-chapter-id="${data.chapter.id}" type="button">重新检查本章</button><button class="button button-secondary regenerate-chapter" data-chapter-id="${data.chapter.id}" type="button">重新生成本章语音</button><button class="button button-secondary merge-chapter" data-chapter-number="${data.chapter.chapter_index + 1}" ${data.segments.every((segment) => segment.has_audio) ? "" : 'disabled title="本章还有分段没有生成语音"'} type="button">重新合并本章</button><button class="text-button close-dialog" type="button">关闭</button></div></div><div class="drawer-segments">${data.segments.map(renderSegmentDetail).join("")}</div>`);
    } catch (error) {
      showToast(error.message);
    }
  }

  function renderSegmentDetail(segment) {
    // 读音检查前展示原文；检查后以朗读稿为准，空朗读稿表示整段不朗读
    const speechText = segment.prechecked ? segment.speech_text : segment.text;
    const speechBlock = speechText ? `<p>${escapeHtml(speechText)}</p>` : `<p class="muted-text">本段都是注释等不适合朗读的内容，生成时为短暂静音。</p>`;
    const sourceBlock = speechText !== segment.text ? `<details class="source-text"><summary>查看原文</summary><p>${escapeHtml(segment.text)}</p></details>` : "";
    const audio = segment.has_audio
      ? `<div class="segment-audio-row"><audio class="segment-audio" controls preload="none" src="/api/segments/${segment.id}/audio?v=${encodeURIComponent(segment.updated_at || "")}">你的浏览器不支持音频播放。</audio><button class="button button-secondary regenerate-segment" data-segment-id="${segment.id}" data-global-index="${segment.global_index}" data-chapter-id="${segment.chapter_id}" type="button">重新生成</button></div>`
      : segment.prechecked
        ? `<button class="button button-primary synthesize-segment" data-global-index="${segment.global_index}" data-chapter-id="${segment.chapter_id}" type="button">生成这段语音</button>`
        : `<span class="segment-audio-empty">完成读音检查后可生成语音</span>`;
    return `<article class="segment-detail ${segment.status}"><div class="segment-detail-head"><span>分段 ${segment.segment_index + 1} · 全书第 ${segment.global_index + 1} 段</span><span>${segment.audio_voice_label ? `<span class="segment-voice">${escapeHtml(segment.audio_voice_label)}</span>` : ""}${segment.taken_over ? `<span class="takeover-badge">本地模型接管</span>` : ""}${escapeHtml(segment.status === "completed" ? "已生成语音" : segment.status === "failed" ? "需要处理" : "等待生成")}</span></div><h3>${segment.prechecked ? "朗读稿" : "原文（尚未检查）"}</h3>${speechBlock}${sourceBlock}${audio}<div class="hint-list">${segment.hints.length ? segment.hints.map(renderHintForm).join("") : `<span class="muted-text">没有发音提示</span>`}</div><button class="button button-secondary add-hint" data-segment-id="${segment.id}" type="button">新增发音提示</button></article>`;
  }

  // 单段生成语音：提交只含这一段的语音窗口，完成后刷新当前章节抽屉以便立即试听
  async function synthesizeSingleSegment(button) {
    const sequence = Number(button.dataset.globalIndex) + 1;
    await runSingleSegment(button, "生成这段语音", () => request(`/api/books/${bookId}/executions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ selection_mode: "advanced", start_segment: sequence, end_segment: sequence, start_stage: "synthesize", end_stage: "synthesize", output_mode: "none" }),
    }));
  }

  // 章节级操作：提交对应的执行窗口后关闭抽屉，由制作台展示进度
  async function submitChapterAction(button, submit, startedMessage) {
    button.disabled = true;
    try {
      await submit();
      button.closest("dialog")?.close();
      showToast(startedMessage);
      openEvents();
      await loadDetail();
    } catch (error) {
      showToast(error.message);
      button.disabled = false;
    }
  }

  // 重新检查整章读音：覆盖本章朗读稿和自动生成的发音提示，手动提示和已生成的语音保留
  async function recheckChapter(button) {
    const confirmed = await confirmDialog({
      title: "重新检查本章",
      message: "本章的朗读稿和自动生成的发音提示会重新生成；手动添加的提示和已生成的语音会保留，需要时可对分段重新生成语音。",
      confirmLabel: "重新检查",
    });
    if (!confirmed) return;
    await submitChapterAction(button, () => request(`/api/chapters/${button.dataset.chapterId}/recheck`, { method: "POST" }), "已开始重新检查本章");
  }

  // 重新生成整章语音：替换本章全部分段音频，完成后自动重新合并章节文件
  async function regenerateChapter(button) {
    const confirmed = await confirmDialog({
      title: "重新生成本章语音",
      message: "本章所有分段的语音会按当前的朗读稿和配音设置重新生成，完成后自动重新合并章节文件。",
      confirmLabel: "重新生成",
    });
    if (!confirmed) return;
    await submitChapterAction(button, () => request(`/api/chapters/${button.dataset.chapterId}/regenerate`, { method: "POST" }), "已开始重新生成本章语音");
  }

  // 重新合并整章：用现有分段语音重新生成章节文件，不重新生成语音
  async function mergeChapter(button) {
    const chapter = Number(button.dataset.chapterNumber);
    await submitChapterAction(button, () => request(`/api/books/${bookId}/executions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ selection_mode: "chapters", chapter_start: chapter, chapter_end: chapter, start_stage: "package", end_stage: "package", output_mode: "chapters" }),
    }), "已开始重新合并本章");
  }

  // 重新生成单段语音：旧音频会被替换，所在章节的成品需要重新导出
  async function regenerateSingleSegment(button) {
    const confirmed = await confirmDialog({
      title: "重新生成这段语音",
      message: "当前这段音频会被替换，所在章节已导出的文件需要重新导出。",
      confirmLabel: "重新生成",
    });
    if (!confirmed) return;
    await runSingleSegment(button, "重新生成", () => request(`/api/segments/${button.dataset.segmentId}/regenerate`, { method: "POST" }));
  }

  async function runSingleSegment(button, idleLabel, submit) {
    const sequence = Number(button.dataset.globalIndex) + 1;
    button.disabled = true;
    button.textContent = "正在生成…";
    try {
      const { execution } = await submit();
      openEvents();
      let current = execution;
      while (["queued", "running"].includes(current.status)) {
        await new Promise((resolve) => window.setTimeout(resolve, 1500));
        current = await request(`/api/executions/${execution.id}`);
      }
      if (current.status !== "completed") showToast(current.error_message || `全书第 ${sequence} 段语音生成未完成`);
      const dialog = button.closest("dialog");
      if (dialog?.isConnected) {
        dialog.close();
        await showChapterSegments(button.dataset.chapterId);
      }
    } catch (error) {
      showToast(error.message);
      button.disabled = false;
      button.textContent = idleLabel;
    }
  }

  function renderHintForm(hint) {
    return `<form class="hint-editor" data-hint-id="${hint.id}"><label>词组<input class="hint-input hint-surface" value="${escapeHtml(hint.surface)}"/></label><label>第几处<input class="hint-input hint-occurrence" type="number" min="0" value="${hint.occurrence_index}"/></label><label class="hint-pronunciation">读音<input class="hint-input hint-pronunciation-input" value="${escapeHtml(hint.pronunciation)}"/></label><label class="hint-kind">类型<input class="hint-input hint-kind-input" value="${escapeHtml(hint.kind)}"/></label><button class="button button-secondary" type="submit">保存</button></form>`;
  }

  function renderProblemDetail(problem) {
    const label = problem.kind === "precheck" ? "朗读稿检查" : problem.kind === "segment" ? "语音分段" : "执行任务";
    const location = problem.chapter_index != null && problem.segment_index != null ? `第 ${problem.chapter_index + 1} 章 · 分段 ${problem.segment_index + 1}` : "";
    const responsePath = problem.response_path || problem.detail_path;
    const contentPath = problem.content_path;
    const diagnosticSections = [
      problem.detail ? `<section class="error-detail-section"><div class="error-detail-section-heading"><h3>服务商原始响应</h3><span>可滚动查看全部内容</span></div><pre class="error-detail-raw">${escapeHtml(problem.detail)}</pre>${responsePath ? `<p class="problem-path">来源：${escapeHtml(responsePath)}</p>` : ""}</section>` : "",
      problem.content_detail ? `<section class="error-detail-section"><div class="error-detail-section-heading"><h3>模型实际输出</h3><span>可滚动查看全部内容</span></div><pre class="error-detail-raw">${escapeHtml(problem.content_detail)}</pre>${contentPath ? `<p class="problem-path">来源：${escapeHtml(contentPath)}</p>` : ""}</section>` : "",
    ].join("");
    return `<article class="error-detail-card"><div class="error-detail-card-heading"><div><strong>${label}</strong>${location ? `<span>${location}</span>` : ""}</div></div><div class="error-detail-message"><strong>完整错误信息</strong><p>${escapeHtml(problem.message || "未知错误")}</p></div>${problem.surface ? `<div class="error-detail-context"><strong>相关词组</strong><p>${escapeHtml(problem.surface)}</p><strong>原文</strong><p>${escapeHtml(problem.source_text || "")}</p></div>` : ""}${diagnosticSections || `<p class="muted-text">当前没有可读取的原始响应文件。</p>`}</article>`;
  }

  async function showExecutionProblem(executionId) {
    try {
      const detail = await request(`/api/executions/${executionId}`);
      const stage = detail.current_stage || detail.end_stage;
      const range = executionRange(detail);
      const problems = detail.problems || [];
      openDialog("drawer-dialog drawer-dialog-wide", `<div class="dialog-header"><div><span class="dialog-kicker">${escapeHtml(stageLabels[stage] || "处理任务")}</span><h2>错误详情</h2></div><button class="text-button close-dialog" type="button">关闭</button></div><p class="muted-text">执行窗口 ${escapeHtml(executionId.slice(0, 8))} · ${escapeHtml(range)}</p><div class="error-detail-list">${problems.length ? problems.map(renderProblemDetail).join("") : `<p class="muted-text">当前没有可展示的问题。</p>`}</div>`);
    } catch (error) {
      showToast(error.message);
    }
  }

  async function handleExecutionAction(event) {
    const pause = event.target.closest(".pause-execution");
    const retry = event.target.closest(".retry-execution");
    const problem = event.target.closest(".view-problem");
    if (!pause && !retry && !problem) return false;
    const button = pause || retry || problem;
    if (problem) { await showExecutionProblem(problem.dataset.executionId); return true; }
    button.disabled = true;
    try {
      if (pause) {
        await request(`/api/executions/${pause.dataset.executionId}/pause`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        showToast("已请求暂停，当前操作完成后生效");
      }
      if (retry) {
        await request(`/api/executions/${retry.dataset.executionId}/retry`, { method: "POST" });
        showToast("这一步已经重新排队");
      }
      await loadDetail();
      openEvents();
    } catch (error) {
      showToast(error.message);
      button.disabled = false;
    }
    return true;
  }

  async function setupBook() {
    restoreWorkbench();
    try {
      await loadTtsProviders();
      await loadDetail();
    } catch (error) {
      const node = document.getElementById("book-error");
      node.textContent = error.message;
      node.classList.remove("hidden");
    }
    openEvents();
    document.addEventListener("click", async (event) => {
      const openRange = event.target.closest(".open-execution-range");
      if (openRange) {
        const data = workbench.latestData;
        const execution = (data.executions || []).find((item) => item.id === openRange.dataset.executionId);
        if (!execution) throw new Error(`找不到执行记录 ${openRange.dataset.executionId}`);
        openExecutionRange(data, execution);
        return;
      }
      const chapter = event.target.closest(".chapter-index-row");
      if (chapter) { await showChapterSegments(chapter.dataset.chapterId); return; }
      const synthesizeSegment = event.target.closest(".synthesize-segment");
      if (synthesizeSegment) { await synthesizeSingleSegment(synthesizeSegment); return; }
      const recheck = event.target.closest(".recheck-chapter");
      if (recheck) { await recheckChapter(recheck); return; }
      const regenerateChapterButton = event.target.closest(".regenerate-chapter");
      if (regenerateChapterButton) { await regenerateChapter(regenerateChapterButton); return; }
      const merge = event.target.closest(".merge-chapter");
      if (merge) { await mergeChapter(merge); return; }
      const regenerateSegment = event.target.closest(".regenerate-segment");
      if (regenerateSegment) { await regenerateSingleSegment(regenerateSegment); return; }
      const addHint = event.target.closest(".add-hint");
      if (addHint) {
        addHint.insertAdjacentHTML("beforebegin", `<form class="hint-editor new-hint" data-segment-id="${addHint.dataset.segmentId}"><label>词组<input class="hint-input hint-surface" placeholder="朗读稿中的词组" required/></label><label>第几处<input class="hint-input hint-occurrence" type="number" min="0" value="0" required/></label><label class="hint-pronunciation">读音<input class="hint-input hint-pronunciation-input" placeholder="带声调拼音" required/></label><label class="hint-kind">类型<input class="hint-input hint-kind-input" value="manual" required/></label><button class="button button-primary" type="submit">添加</button></form>`);
        addHint.remove();
        return;
      }
      await handleExecutionAction(event);
    });
    document.addEventListener("submit", async (event) => {
      const form = event.target.closest(".hint-editor");
      if (!form) return;
      event.preventDefault();
      const values = {
        surface: form.querySelector(".hint-surface").value,
        occurrence_index: Number(form.querySelector(".hint-occurrence").value),
        pronunciation: form.querySelector(".hint-pronunciation-input").value,
        kind: form.querySelector(".hint-kind-input").value,
      };
      try {
        if (form.classList.contains("new-hint")) {
          values.segment_id = Number(form.dataset.segmentId);
          await request(`/api/books/${bookId}/hints`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values) });
        } else {
          await request(`/api/hints/${form.dataset.hintId}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values) });
        }
        showToast("发音提示已保存，对应分段会重新生成");
        form.closest("dialog")?.close();
        await loadDetail();
      } catch (error) {
        showToast(error.message);
      }
    });
  }

  if (page === "dashboard") setupDashboard();
  if (page === "book") setupBook();
})();
