(function () {
  'use strict';

  var VOICE_LABELS = {
    'zh-CN-YunyangNeural': '云扬 · 中文男声',
    'zh-CN-XiaoxiaoNeural': '晓晓 · 中文女声'
  };

  function chinaTime(value) {
    if (!value) return '未知';
    var parsed = new Date(value);
    if (!Number.isFinite(parsed.getTime())) return '未知';
    return new Intl.DateTimeFormat('sv-SE', {
      timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
    }).format(parsed);
  }

  function reportDate() {
    var pathMatch = window.location.pathname.match(/\/reports\/(\d{4}-\d{2}-\d{2})(?:\/|\.html)/);
    if (pathMatch) return pathMatch[1];
    var generated = Array.from(document.querySelectorAll('.info-item')).find(function (item) {
      var label = item.querySelector('.info-label');
      return label && label.textContent.trim() === '生成时间';
    });
    var value = generated && generated.querySelector('.info-value');
    var dateMatch = value && value.textContent.match(/(\d{2}-\d{2})/);
    return dateMatch ? dateMatch[1] : '';
  }

  function matchesReport(metadata) {
    var current = reportDate();
    if (!current || !metadata.date) return false;
    return current.length === 5 ? metadata.date.slice(5) === current : metadata.date === current;
  }

  function metadataUrl() {
    var pathMatch = window.location.pathname.match(/\/reports\/(\d{4}-\d{2}-\d{2})(?:\/|\.html)/);
    return pathMatch ? '/audio/' + pathMatch[1] + '/daily.json' : '/audio/latest.json';
  }

  // Same-device, same-origin resume only. Full page navigation stops audio;
  // the next page restores on metadata and requires a fresh user play gesture.
  var STORAGE_KEY = 'newsradar.podcast.v1';
  var RATES = [0.8, 1, 1.25, 1.5, 2];
  var RETENTION = 90 * 24 * 60 * 60 * 1000;

  function readProgress() {
    var data;
    try { data = JSON.parse(window.localStorage.getItem(STORAGE_KEY)); } catch (_) {}
    var now = Date.now();
    return {
      rate: data && RATES.indexOf(data.rate) !== -1 ? data.rate : 1,
      entries: data && Array.isArray(data.entries) ? data.entries.filter(function (entry) {
        return entry && typeof entry.key === 'string' &&
          Number.isFinite(entry.position) && entry.position >= 0 &&
          Number.isFinite(entry.updatedAt) && entry.updatedAt > now - RETENTION && entry.updatedAt <= now;
      }).sort(function (a, b) { return b.updatedAt - a.updatedAt; }).slice(0, 100) : []
    };
  }

  function writeProgress(data) {
    try { window.localStorage.setItem(STORAGE_KEY, JSON.stringify(data)); return true; }
    catch (_) { return false; }
  }

  function clampSeek(audio, seconds) {
    if (!Number.isFinite(seconds) || !Number.isFinite(audio.duration) || audio.duration <= 0) return 0;
    return Math.max(0, Math.min(seconds, Math.max(0, audio.duration - 0.25)));
  }

  function timeLabel(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    return Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0');
  }

  function setupContinuity(audio, body, metadata) {
    var key = JSON.stringify([metadata.date, metadata.audio_url, metadata.source_hash || 'legacy']);
    var data = readProgress();
    var saved = data.entries.find(function (entry) { return entry.key === key; });
    var position = saved && !saved.completed ? saved.position : 0;
    var completed = Boolean(saved && saved.completed);
    var restored = false;
    var lastSave = -Infinity;
    var progressDirty = false;
    var rateDirty = false;
    var localRate = data.rate;
    // Programmatic restoration is a baseline, not a local listening/seek edit.
    var observedTime = audio.currentTime;
    var controls = document.createElement('div');
    controls.className = 'podcast-controls';
    var label = document.createElement('label');
    label.className = 'podcast-rate-label';
    label.textContent = '播放速度';
    var select = document.createElement('select');
    select.className = 'podcast-rate';
    select.setAttribute('aria-label', '播放速度');
    RATES.forEach(function (rate) {
      var option = document.createElement('option');
      option.value = String(rate);
      option.textContent = rate + '×';
      select.appendChild(option);
    });
    select.value = String(data.rate);
    audio.playbackRate = data.rate;
    label.appendChild(select);
    var resume = document.createElement('button');
    resume.type = 'button';
    resume.className = 'podcast-resume';
    var note = document.createElement('p');
    note.className = 'podcast-resume-note';
    note.textContent = '进度仅保存在本设备浏览器；切换页面会停止播放，返回后点击继续收听。';
    function updateResume() {
      resume.hidden = !(position > 0) || completed;
      resume.textContent = '继续收听 · ' + timeLabel(position);
      resume.setAttribute('aria-label', '从 ' + timeLabel(position) + ' 继续收听');
    }
    updateResume();
    controls.append(label, resume);
    body.append(controls, note);

    function restore() {
      if (restored || !Number.isFinite(audio.duration) || audio.duration <= 0) return;
      restored = true;
      position = clampSeek(audio, position);
      if (position > 0) audio.currentTime = position;
      observedTime = audio.currentTime;
      updateResume();
    }
    function capturePositionChange() {
      if (!restored || !Number.isFinite(audio.currentTime)) return;
      if (audio.currentTime !== observedTime) {
        progressDirty = true;
        observedTime = audio.currentTime;
      }
    }
    function syncInactive(latest) {
      if (!audio.paused || progressDirty) return;
      var entry = latest.entries.find(function (item) { return item.key === key; });
      position = entry && !entry.completed ? entry.position : 0;
      completed = Boolean(entry && entry.completed);
      // Keep this as a pending resume target; do not seek or play on a storage event.
      if (!rateDirty) {
        localRate = latest.rate;
        select.value = String(localRate);
        if (audio.playbackRate !== localRate) audio.playbackRate = localRate;
      }
      updateResume();
    }
    function save(force) {
      capturePositionChange();
      var now = Date.now();
      if (!force && now - lastSave < 5000) return;
      // Always merge with current storage: a stale untouched tab owns no position.
      data = readProgress();
      if (!progressDirty && !rateDirty) {
        syncInactive(data);
        return;
      }
      if (progressDirty) {
        if (restored) position = completed ? 0 : clampSeek(audio, audio.currentTime);
        data.entries = data.entries.filter(function (entry) { return entry.key !== key; });
        data.entries.unshift({ key: key, position: position, completed: completed, updatedAt: now });
        data.entries = data.entries.slice(0, 100);
      }
      if (rateDirty) data.rate = localRate;
      if (writeProgress(data)) {
        progressDirty = false;
        rateDirty = false;
        syncInactive(data);
      } else {
        note.textContent = '浏览器无法保存进度；本次仍可播放和调速，刷新后可能无法恢复。';
      }
      lastSave = now;
      updateResume();
    }
    function changeRate(rate) {
      if (RATES.indexOf(rate) === -1 || rate === localRate) return;
      localRate = rate;
      rateDirty = true;
      select.value = String(rate);
      audio.playbackRate = rate;
      save(true);
    }
    audio.addEventListener('loadedmetadata', restore);
    select.addEventListener('change', function () { changeRate(Number(select.value)); });
    audio.addEventListener('ratechange', function () { changeRate(audio.playbackRate); });
    resume.addEventListener('click', function () {
      function playSaved() {
        restore();
        if (restored) audio.currentTime = clampSeek(audio, position);
        audio.play().catch(function () {});
      }
      if (audio.readyState >= 1) playSaved();
      else {
        audio.addEventListener('loadedmetadata', playSaved, { once: true });
        audio.load();
      }
    });
    audio.addEventListener('play', function () { completed = false; });
    audio.addEventListener('timeupdate', function () { save(false); });
    audio.addEventListener('seeked', function () {
      capturePositionChange();
      if (progressDirty) completed = false;
      save(true);
    });
    audio.addEventListener('pause', function () { save(true); });
    audio.addEventListener('ended', function () { completed = true; position = 0; progressDirty = true; save(true); });
    if (window.addEventListener) {
      window.addEventListener('pagehide', function () { save(true); });
      window.addEventListener('storage', function (event) {
        if (event.key !== STORAGE_KEY && event.key !== null) return;
        capturePositionChange();
        syncInactive(readProgress());
      });
      // Capture navigation before the next document takes over, including history links.
      document.addEventListener('click', function (event) {
        var link = event.target.closest && event.target.closest('a[href]');
        if (link) save(true);
      }, true);
    }
    if (audio.readyState >= 1) restore();
  }

  function panelMarkup(metadata, available) {
    var section = document.createElement('section');
    section.className = 'speech-panel';
    section.setAttribute('aria-labelledby', 'podcastTitle');
    var heading = document.createElement('div');
    heading.className = 'speech-panel-head';
    heading.innerHTML = '<h2 class="speech-panel-title" id="podcastTitle">今日 AI 新闻播报</h2>' +
      '<span class="speech-badge">AI 播客</span>';
    var reportStamp = document.createElement('p');
    reportStamp.className = 'podcast-report-time';
    var reportInfo = Array.from(document.querySelectorAll('.info-item')).find(function (item) {
      var label = item.querySelector('.info-label');
      return label && label.textContent.trim() === '生成时间';
    });
    var reportValue = reportInfo && reportInfo.querySelector('.info-value');
    reportStamp.textContent = '新闻报告更新：' + (reportValue ? reportValue.textContent.trim() + '（北京时间）' : '请查看报告顶部生成时间');
    var body = document.createElement('div');
    if (available) {
      body.className = 'podcast-slot';
      var audio = document.createElement('audio');
      audio.className = 'podcast-audio';
      audio.controls = true;
      audio.preload = 'metadata';
      audio.src = metadata.audio_url;
      audio.setAttribute('aria-label', metadata.date + ' AI 新闻播报');
      var meta = document.createElement('span');
      meta.className = 'podcast-meta';
      meta.textContent = '播报日期：' + metadata.date + ' · ' +
        (metadata.voice_label || VOICE_LABELS[metadata.voice] || '自然中文语音');
      var generated = document.createElement('span');
      generated.className = 'podcast-generated';
      generated.textContent = '音频生成：' + chinaTime(metadata.generated_at) + '（北京时间）';
      meta.appendChild(generated);
      if (metadata.source_reason || metadata.source_kind === 'legacy_unverified') {
        var grounding = document.createElement('span');
        grounding.className = 'podcast-generated';
        grounding.textContent = metadata.source_reason || '旧版口播缺少结构化依据，关键事实请核对原文。';
        meta.appendChild(grounding);
      }
      if (Number.isFinite(metadata.duration_seconds)) {
        var duration = document.createElement('span');
        duration.className = 'podcast-generated';
        duration.textContent = '实测时长：' + Math.round(metadata.duration_seconds) + ' 秒';
        meta.appendChild(duration);
      }
      body.append(audio, meta);
      setupContinuity(audio, body, metadata);
      if (Array.isArray(metadata.chapters) && metadata.chapters.length) {
        var chapters = document.createElement('div');
        chapters.className = 'podcast-chapters';
        chapters.setAttribute('aria-label', '播客章节');
        metadata.chapters.forEach(function (chapter) {
          var button = document.createElement('button');
          button.type = 'button';
          button.className = 'podcast-chapter';
          button.textContent = chapter.title;
          button.setAttribute('aria-label', '跳转到：' + chapter.title);
          button.setAttribute('aria-pressed', 'false');
          button.addEventListener('click', function () {
            function seekAndPlay() {
              var ratio = Number(chapter.start_ratio);
              var seconds = Number(chapter.start_seconds || 0);
              var target = Number.isFinite(ratio) && Number.isFinite(audio.duration)
                ? ratio * audio.duration
                : seconds;
              if (!Number.isFinite(audio.duration) || audio.duration <= 0) return;
              audio.currentTime = clampSeek(audio, target);
              chapters.querySelectorAll('.podcast-chapter').forEach(function (item) {
                item.setAttribute('aria-pressed', String(item === button));
              });
              audio.play().catch(function () {});
            }
            if (audio.readyState >= 1) seekAndPlay();
            else {
              audio.addEventListener('loadedmetadata', seekAndPlay, { once: true });
              audio.load();
            }
          });
          chapters.appendChild(button);
        });
        body.appendChild(chapters);
      }
      if ('IntersectionObserver' in window) {
        var visible = true;
        function updateMini() {
          var mini = !visible && !audio.paused && !audio.ended;
          // Reserve both the original slot and enough document tail for fixed controls.
          if (mini && !body.classList.contains('is-mini')) {
            section.style.minHeight = section.getBoundingClientRect().height + 'px';
          }
          body.classList.toggle('is-mini', mini);
          var height = mini ? body.getBoundingClientRect().height + 32 : 0;
          document.documentElement.style.setProperty('--podcast-mini-space', height + 'px');
          document.documentElement.classList.toggle('has-podcast-mini', mini);
          if (!mini) section.style.minHeight = '';
        }
        var observer = new IntersectionObserver(function (entries) {
          visible = entries[0].isIntersecting;
          updateMini();
        }, { threshold: 0.15 });
        observer.observe(section);
        ['play', 'pause', 'ended'].forEach(function (event) { audio.addEventListener(event, updateMini); });
        window.addEventListener('resize', updateMini);
        if ('ResizeObserver' in window) {
          new ResizeObserver(updateMini).observe(body);
        }
      }
    } else {
      body.className = 'podcast-empty';
      var message = document.createElement('strong');
      message.textContent = '今日播客暂未生成';
      var detail = document.createElement('span');
      detail.textContent = metadata && metadata.date
        ? '最近一期为 ' + metadata.date + '，请前往对应历史报告收听。'
        : '当天 AI 日报完成后会自动生成并显示在这里。';
      body.append(message, detail);
    }
    section.append(heading, reportStamp, body);
    return section;
  }

  function init() {
    var content = document.querySelector('.content');
    if (!content || !window.fetch) return;
    fetch(metadataUrl(), { cache: 'no-store' })
      .then(function (response) {
        if (!response.ok) throw new Error('unavailable');
        return response.json();
      })
      .then(function (metadata) {
        var available = Boolean(metadata.audio_url && matchesReport(metadata));
        content.insertBefore(panelMarkup(metadata, available), content.firstChild);
      })
      .catch(function () {
        content.insertBefore(panelMarkup(null, false), content.firstChild);
      });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
