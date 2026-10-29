(function () {
  'use strict';

  var DEFAULT_RECOMMENDATION_LIMIT = 5;
  var state = { data: null, articles: new Map(), active: 'global_important', expanded: false };

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (character) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character];
    });
  }

  function normalizedUrl(value) {
    try {
      var url = new URL(value, window.location.origin);
      ['fbclid', 'gclid', 'ref', 'referrer', 'source', 'spm', 'utm_campaign', 'utm_content', 'utm_medium', 'utm_source', 'utm_term']
        .forEach(function (key) { url.searchParams.delete(key); });
      url.hash = '';
      return url.href.replace(/\/$/, '');
    } catch (_error) {
      return value || '';
    }
  }

  function reportDate() {
    var match = window.location.pathname.match(/\/reports\/(\d{4}-\d{2}-\d{2})(?:\/|\.html)/);
    if (match) return match[1];
    return state.data ? state.data.as_of : '';
  }

  function safeHref(value) {
    try {
      var url = new URL(value, window.location.origin);
      return /^https?:$/.test(url.protocol) && !url.username && !url.password ? url.href : '#';
    } catch (_error) { return '#'; }
  }

  function editorialHtml(article) {
    var assessment = article.assessment || {};
    var detail = article.editorial || {};
    var materials = article.primary_materials || [];
    var evidence = '<a href="' + escapeHtml(safeHref(article.url || article.report_url)) + '" target="_blank" rel="noopener noreferrer">原始报道</a>';
    materials.forEach(function (material) {
      evidence += ' · <a href="' + escapeHtml(safeHref(material.url)) + '" target="_blank" rel="noopener noreferrer">原站材料候选</a>';
    });
    var values = '<dt>发生什么</dt><dd>' + escapeHtml(article.title) + '</dd>';
    if (assessment.reason && assessment.decision !== 'legacy_unknown') values += '<dt>为何关注</dt><dd>' + escapeHtml(assessment.reason) + '</dd>';
    if (detail.interpretation) values += '<dt>AI 判断（推测）</dt><dd>' + escapeHtml(detail.interpretation) + '</dd>';
    values += '<dt>事实依据</dt><dd>' + evidence + '</dd>';
    values += '<dt>证据局限</dt><dd>' + escapeHtml(detail.uncertainty || '标题或摘要为来源陈述；原站材料与新闻的关联、来源独立性仍需核对。') + '</dd>';
    if (typeof assessment.information_value === 'number') values += '<dt>信息价值（AI 自评）</dt><dd>' + Math.round(assessment.information_value * 100) + '% · 主题相关度 ' + Math.round(assessment.topic_relevance * 100) + '%</dd>';
    else values += '<dt>信息价值</dt><dd>旧分类未评分</dd>';
    return '<details class="editorial-detail"><summary>重点依据与工程影响</summary><dl class="editorial-facts">' + values + '</dl></details>';
  }

  function articleLink(article) {
    var href = safeHref(article.url || article.report_url);
    return '<a href="' + escapeHtml(href) + '" target="_blank" rel="noopener noreferrer">' +
      escapeHtml(article.title) + '</a>';
  }

  function renderList() {
    var root = document.getElementById('experienceList');
    if (!root || !state.data) return;
    var ids = state.data.recommendations[state.active] || [];
    var articles = ids.map(function (id) { return state.articles.get(id); }).filter(function (article) { return article && article.date === reportDate(); });
    var visibleArticles = state.expanded ? articles : articles.slice(0, DEFAULT_RECOMMENDATION_LIMIT);
    root.innerHTML = visibleArticles.map(function (article) {
      return '<article class="experience-card">' +
        '<div class="experience-card-head"><div>' + articleLink(article) +
        '<p class="experience-meta">' + escapeHtml(article.source) + ' · ' + escapeHtml(article.date) +
        ' · ' + escapeHtml(article.score.reason) + '</p></div>' +
        '<span class="experience-score" title="综合重要性评分">' + article.score.total + '</span></div>' +
        (article.progress ? '<p class="experience-progress">' + escapeHtml(article.progress.label) + '</p>' : '') +
        (article.syndication ? '<p class="experience-meta">来源独立性未核验</p>' : '') + editorialHtml(article) +
        '<div class="experience-actions"><a href="/events.html#' + escapeHtml(article.event_id) + '">查看事件与依据</a></div>' +
        '</article>';
    }).join('') || '<p>当前没有可展示的重点新闻。</p>';
    renderExpandControl(articles.length);
  }

  function renderExpandControl(total) {
    var button = document.querySelector('.experience-expand');
    if (!button) return;
    button.hidden = total <= DEFAULT_RECOMMENDATION_LIMIT;
    button.setAttribute('aria-expanded', String(state.expanded));
    button.textContent = state.expanded
      ? '收起到 ' + DEFAULT_RECOMMENDATION_LIMIT + ' 条'
      : '展开全部 ' + total + ' 条';
  }

  function renderPanel() {
    var content = document.querySelector('.content');
    if (!content || document.querySelector('.experience-panel')) return;
    var claims = (state.data.claims || []).filter(function (claim) {
      var evidence = claim.evidence_ids.map(function (id) { return state.articles.get(id); }).filter(Boolean);
      return evidence.some(function (article) { return article.date === reportDate(); });
    });
    var lead = claims[0];
    var leadEvent = lead && (state.data.events || []).find(function (event) { return event.id === lead.event_id; });
    var panel = document.createElement('section');
    panel.className = 'experience-panel';
    panel.setAttribute('aria-labelledby', 'experienceTitle');
    panel.innerHTML = '<p class="experience-kicker">AI DAILY BRIEF · ' + escapeHtml(reportDate()) + '</p>' +
      '<h2 id="experienceTitle">今天先看这些</h2>' +
      '<p class="experience-summary">' + escapeHtml(
        leadEvent
          ? '今日最值得关注：' + leadEvent.title.replace(/[。！？!?]+$/, '') + '。' + lead.statement + '。'
          : '今日重点已按重要性、兴趣匹配、热度与时效综合整理。'
      ) + '</p>' +
      '<p class="experience-disclaimer">AI 判断会明确标注并关联事实依据；仍建议打开原始来源核验关键细节。</p>' +
      '<div class="experience-tabs" role="tablist" aria-label="重点新闻类型">' +
      '<button class="experience-tab" role="tab" data-kind="global_important" aria-selected="true">全局重要</button>' +
      '<button class="experience-tab" role="tab" data-kind="for_you" aria-selected="false">为你推荐</button></div>' +
      '<div class="experience-list" id="experienceList"></div>' +
      '<div class="experience-actions"><button class="experience-expand" type="button" hidden></button>' +
      '<a href="/search.html">搜索全部历史</a><a href="/events.html">查看事件追踪</a><a href="/trends.html">查看趋势</a></div>';
    var insertion = content.querySelector('.search-bar') || content.firstChild;
    content.insertBefore(panel, insertion);
    panel.querySelectorAll('.experience-tab').forEach(function (button) {
      button.addEventListener('click', function () {
        state.active = button.dataset.kind;
        state.expanded = false;
        panel.querySelectorAll('.experience-tab').forEach(function (item) {
          item.setAttribute('aria-selected', String(item === button));
        });
        renderList();
      });
    });
    panel.querySelector('.experience-expand').addEventListener('click', function () {
      state.expanded = !state.expanded;
      renderList();
    });
    renderList();
  }

  function annotateReportItems() {
    var byUrl = new Map();
    state.data.articles.forEach(function (article) {
      if (article.url) byUrl.set(normalizedUrl(article.url), article);
    });
    document.querySelectorAll('.news-item, .new-item').forEach(function (item) {
      var anchor = item.querySelector('a.news-link');
      var article = anchor && byUrl.get(normalizedUrl(anchor.href));
      if (!article || item.querySelector('.news-evidence-toggle')) return;
      item.dataset.newsId = article.id;
      item.id = item.id || 'news-' + article.id;
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'news-evidence-toggle';
      button.textContent = '查看推荐依据';
      button.setAttribute('aria-expanded', 'false');
      var detail = document.createElement('div');
      detail.className = 'news-evidence';
      detail.hidden = true;
      detail.innerHTML = '<strong>' + escapeHtml(article.score.reason) + '</strong><br>' +
        '综合评分 ' + article.score.total + ' · 兴趣 ' + article.score.components.interest +
        ' · 热榜 ' + article.score.components.rank + ' · 时效 ' + article.score.components.freshness +
        '<br><a href="/events.html#' + escapeHtml(article.event_id) + '">查看事件时间线和事实依据</a>';
      var body = item.querySelector('.news-content, .new-item-content') || item;
      body.append(button, detail);
      button.addEventListener('click', function () {
        var before = item.getBoundingClientRect().top;
        detail.hidden = !detail.hidden;
        button.setAttribute('aria-expanded', String(!detail.hidden));
        button.textContent = detail.hidden ? '查看推荐依据' : '收起推荐依据';
        window.scrollBy(0, item.getBoundingClientRect().top - before);
      });
    });
  }

  function init() {
    if (!window.fetch) return;
    fetch('/experience.json', { cache: 'no-store' }).then(function (response) {
      if (!response.ok) throw new Error('experience unavailable');
      return response.json();
    }).then(function (data) {
      state.data = data;
      data.articles.forEach(function (article) { state.articles.set(article.id, article); });
      renderPanel();
      annotateReportItems();
      // Optional canonical brief; old historical pages never inherit today's claims.
      fetch('/facts/' + reportDate() + '.json', { cache: 'no-store' }).then(function (response) {
        if (!response.ok) throw new Error('facts unavailable');
        return response.json();
      }).then(function (pack) {
        if (pack.date !== reportDate()) return;
        var facts = new Map((pack.articles || []).map(function (item) { return [normalizedUrl(item.url), item]; }));
        var screening = new Map((pack.screening_decisions || []).map(function (item) { return [item.title, item]; }));
        data.articles.forEach(function (article) {
          if (article.date === pack.date && screening.has(article.title)) article.assessment = screening.get(article.title);
          var fact = facts.get(normalizedUrl(article.url));
          if (!fact || fact.date !== article.date) return;
          ['assessment', 'progress', 'primary_materials', 'syndication'].forEach(function (key) { article[key] = fact[key]; });
          var sourceIds = Object.keys(pack.evidence_sources || {}).filter(function (id) {
            return normalizedUrl(pack.evidence_sources[id].url) === normalizedUrl(article.url);
          });
          var claim = (pack.claims || []).find(function (item) { return item.source_ids.some(function (id) { return sourceIds.includes(id); }); });
          if (claim) article.editorial = claim;
        });
        Object.keys(data.recommendations).forEach(function (kind) {
          data.recommendations[kind] = data.recommendations[kind].filter(function (id) {
            var article = state.articles.get(id);
            return !article || !article.assessment || !['reject', 'rejected'].includes(article.assessment.decision);
          });
        });
        renderList();
      }).catch(function () { /* Canonical materials are optional for old reports. */ });
    }).catch(function () { /* Existing report remains fully usable. */ });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
