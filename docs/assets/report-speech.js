(function () {
  'use strict';

  var VOICE_LABELS = {
    'zh-CN-YunyangNeural': '云扬 · 中文男声',
    'zh-CN-XiaoxiaoNeural': '晓晓 · 中文女声'
  };

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

  function panelMarkup(metadata, available) {
    var section = document.createElement('section');
    section.className = 'speech-panel';
    section.setAttribute('aria-labelledby', 'podcastTitle');
    var heading = document.createElement('div');
    heading.className = 'speech-panel-head';
    heading.innerHTML = '<h2 class="speech-panel-title" id="podcastTitle">今日 AI 新闻播报</h2>' +
      '<span class="speech-badge">AI 播客</span>';
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
      meta.textContent = metadata.date + ' · ' +
        (metadata.voice_label || VOICE_LABELS[metadata.voice] || '自然中文语音');
      body.append(audio, meta);
      if (Array.isArray(metadata.chapters) && metadata.chapters.length) {
        var chapters = document.createElement('div');
        chapters.className = 'podcast-chapters';
        chapters.setAttribute('aria-label', '播客章节');
        metadata.chapters.forEach(function (chapter) {
          var button = document.createElement('button');
          button.type = 'button';
          button.className = 'podcast-chapter';
          button.textContent = chapter.title;
          button.addEventListener('click', function () {
            audio.currentTime = Number(chapter.start_seconds || 0);
            audio.play().catch(function () {});
          });
          chapters.appendChild(button);
        });
        body.appendChild(chapters);
      }
      if ('IntersectionObserver' in window) {
        audio.addEventListener('play', function () {
          var observer = new IntersectionObserver(function (entries) {
            body.classList.toggle('is-mini', !entries[0].isIntersecting && !audio.paused);
          }, { threshold: 0.15 });
          observer.observe(section);
          audio.addEventListener('ended', function () {
            body.classList.remove('is-mini');
            observer.disconnect();
          }, { once: true });
        }, { once: true });
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
    section.append(heading, body);
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
