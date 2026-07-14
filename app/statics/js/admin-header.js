window.renderAdminHeader = async function renderAdminHeader() {
  const mount = document.getElementById('admin-header');
  if (!mount || mount.children.length) return;
  const scriptVersion = (() => {
    try {
      const script = document.querySelector('script[src*="/static/js/admin-header.js"]');
      if (!script) return 'v1';
      return new URL(script.src, window.location.href).searchParams.get('v') || 'v1';
    } catch {
      return 'v1';
    }
  })();
  // headerCacheRev: bump when header.html markup changes so sessionStorage cannot stick to dead nodes like #hd-version-tag
  const headerCacheRev = '20260713b';
  const HEADER_HTML_CACHE_KEY = `grok2api.admin_header_html.${scriptVersion}.${headerCacheRev}`;
  const META_VERSION_CACHE_KEY = `grok2api.meta_version.${scriptVersion}`;
  let appVersion = '';
  let updateInfo = null;
  let updateStatus = 'idle';
  let updatePromise = null;
  let applyStatus = 'idle'; // idle|loading|ok|error
  let applyResult = null;

  const readSessionCache = (key) => {
    try {
      return sessionStorage.getItem(key) || '';
    } catch {
      return '';
    }
  };

  const writeSessionCache = (key, value) => {
    if (!value) return;
    try {
      sessionStorage.setItem(key, value);
    } catch {}
  };

  const languageCodes = {
    zh: 'CN',
    en: 'EN',
    ja: 'JA',
    es: 'ES',
    de: 'DE',
    fr: 'FR',
  };

  const initLanguageMenu = () => {
    const menu = mount.querySelector('#hd-lang-menu');
    const trigger = mount.querySelector('#hd-lang-trigger');
    const code = mount.querySelector('#hd-lang-code');
    const options = Array.from(mount.querySelectorAll('.admin-lang-option'));
    if (!menu || !trigger || !code || !options.length) return;

    const close = () => {
      menu.classList.remove('open');
      trigger.setAttribute('aria-expanded', 'false');
    };

    const sync = () => {
      const current = window.I18n?.getLang?.() || localStorage.getItem('grok2api_lang') || 'zh';
      code.textContent = languageCodes[current] || current.toUpperCase();
      options.forEach((option) => {
        option.classList.toggle('active', option.dataset.lang === current);
      });
    };

    trigger.addEventListener('click', (event) => {
      event.stopPropagation();
      const open = !menu.classList.contains('open');
      menu.classList.toggle('open', open);
      trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
    });

    options.forEach((option) => {
      option.addEventListener('click', () => {
        const lang = option.dataset.lang;
        if (!lang) return;
        close();
        if (window.I18n?.setLang) {
          I18n.setLang(lang);
        } else {
          localStorage.setItem('grok2api_lang', lang);
          location.reload();
        }
      });
    });

    document.addEventListener('click', (event) => {
      const target = event.target;
      if (!(target instanceof Node) || !menu.contains(target)) close();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') close();
    });

    sync();
    return sync;
  };

  const applyHeaderI18n = () => {
    if (window.I18n?.apply) I18n.apply(mount);
    const trigger = mount.querySelector('#hd-lang-trigger');
    if (trigger) {
      const label = window.t ? t('header.languageLabel') : 'Language';
      trigger.title = label;
      trigger.setAttribute('aria-label', label);
    }
    const logout = mount.querySelector('#hd-logout');
    if (logout) {
      const label = window.t ? t('header.logout') : 'Logout';
      logout.title = label;
      logout.setAttribute('aria-label', label);
    }
  };

  const loadVersion = async () => {
    const cachedVersion = window.__grok2apiMetaVersion || readSessionCache(META_VERSION_CACHE_KEY);
    if (cachedVersion) {
      appVersion = String(cachedVersion).trim();
      window.__grok2apiMetaVersion = appVersion;
      return;
    }
    try {
      const res = await fetch('/meta');
      if (!res.ok) throw new Error('meta unavailable');
      const data = await res.json();
      appVersion = String(data?.version || '').trim();
      window.__grok2apiMetaVersion = appVersion;
      writeSessionCache(META_VERSION_CACHE_KEY, appVersion);
    } catch {
      appVersion = '';
    }
  };

  const refreshUpdate = async (force = false) => {
    if (updatePromise) return updatePromise;
    if (force) updateInfo = null;
    updateStatus = 'loading';
    updatePromise = (async () => {
      try {
        const path = force ? '/meta/update?force=true' : '/meta/update';
        const res = await fetch(path, { cache: 'no-store' });
        if (!res.ok) throw new Error('update unavailable');
        const data = await res.json();
        updateInfo = data && typeof data === 'object' ? data : null;
        updateStatus = 'ready';
      } catch {
        updateInfo = null;
        updateStatus = 'error';
      }
    })().finally(() => {
      updatePromise = null;
    });
    return updatePromise;
  };

  const text = (key, fallback, params) => {
    if (typeof window.t !== 'function') return fallback;
    const value = t(key, params);
    return value === key ? fallback : value;
  };

  const formatDateTime = (value) => {
    if (!value) return '-';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString();
  };

  const escapeHtml = (value) => String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');

  const sanitizeUrl = (value) => {
    try {
      const url = new URL(value, window.location.origin);
      return ['http:', 'https:', 'mailto:'].includes(url.protocol) ? url.href : '';
    } catch {
      return '';
    }
  };

  const sanitizeRenderedHtml = (html) => {
    const template = document.createElement('template');
    template.innerHTML = html;
    const blockedTags = new Set(['script', 'style', 'iframe', 'object', 'embed', 'link', 'meta']);

    const walk = (node) => {
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      const el = node;
      const tag = el.tagName.toLowerCase();

      if (blockedTags.has(tag)) {
        el.remove();
        return;
      }

      Array.from(el.attributes).forEach((attr) => {
        const name = attr.name.toLowerCase();
        const value = attr.value || '';
        if (name.startsWith('on')) {
          el.removeAttribute(attr.name);
          return;
        }
        if ((name === 'href' || name === 'src') && !sanitizeUrl(value)) {
          el.removeAttribute(attr.name);
          return;
        }
        if (name === 'target') {
          el.setAttribute('target', '_blank');
        }
      });

      Array.from(el.children).forEach((child) => walk(child));
    };

    Array.from(template.content.children).forEach((child) => walk(child));
    return template.innerHTML;
  };

  const renderInlineMarkdown = (source) => {
    let html = escapeHtml(source);
    html = html.replace(/`([^`]+)`/g, (_, code) => `<code>${code}</code>`);
    html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, href) => {
      const safeHref = sanitizeUrl(href.trim());
      const safeLabel = escapeHtml(label.trim() || href.trim());
      return safeHref
        ? `<a href="${escapeHtml(safeHref)}" target="_blank" rel="noreferrer">${safeLabel}</a>`
        : safeLabel;
    });
    html = html.replace(/(^|[\s(>])((https?:\/\/|mailto:)[^\s<]+)/g, (_, prefix, rawUrl) => {
      const safeHref = sanitizeUrl(rawUrl.trim());
      if (!safeHref) return `${prefix}${rawUrl}`;
      return `${prefix}<a href="${escapeHtml(safeHref)}" target="_blank" rel="noreferrer">${escapeHtml(rawUrl)}</a>`;
    });
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/(^|[^\*])\*([^*]+)\*/g, '$1<em>$2</em>');
    return html;
  };

  const renderMarkdown = (source) => {
    const lines = String(source || '').replace(/\r\n?/g, '\n').split('\n');
    const html = [];
    const paragraph = [];
    let listType = '';
    let listItems = [];
    let inCodeBlock = false;
    let codeLines = [];
    let quoteLines = [];

    const flushParagraph = () => {
      if (!paragraph.length) return;
      html.push(`<p>${renderInlineMarkdown(paragraph.map((line) => line.trim()).join(' '))}</p>`);
      paragraph.length = 0;
    };

    const flushList = () => {
      if (!listItems.length) return;
      html.push(`<${listType}>${listItems.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join('')}</${listType}>`);
      listItems = [];
      listType = '';
    };

    const flushCodeBlock = () => {
      if (!inCodeBlock) return;
      html.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
      inCodeBlock = false;
      codeLines = [];
    };

    const flushQuote = () => {
      if (!quoteLines.length) return;
      html.push(`<blockquote>${renderInlineMarkdown(quoteLines.map((line) => line.trim()).join(' '))}</blockquote>`);
      quoteLines = [];
    };

    for (const line of lines) {
      if (line.startsWith('```')) {
        flushParagraph();
        flushList();
        flushQuote();
        if (inCodeBlock) {
          flushCodeBlock();
        } else {
          inCodeBlock = true;
          codeLines = [];
        }
        continue;
      }

      if (inCodeBlock) {
        codeLines.push(line);
        continue;
      }

      const trimmed = line.trim();
      const headingMatch = trimmed.match(/^(#{1,6})\s+(.*)$/);
      const unorderedMatch = trimmed.match(/^[-*+]\s+(.*)$/);
      const orderedMatch = trimmed.match(/^\d+\.\s+(.*)$/);
      const quoteMatch = trimmed.match(/^>\s?(.*)$/);

      if (!trimmed) {
        flushParagraph();
        flushList();
        flushQuote();
        continue;
      }

      if (headingMatch) {
        flushParagraph();
        flushList();
        flushQuote();
        const level = headingMatch[1].length;
        html.push(`<h${level}>${renderInlineMarkdown(headingMatch[2])}</h${level}>`);
        continue;
      }

      if (unorderedMatch || orderedMatch) {
        flushParagraph();
        flushQuote();
        const nextType = unorderedMatch ? 'ul' : 'ol';
        const itemText = unorderedMatch ? unorderedMatch[1] : orderedMatch[1];
        if (listType && listType !== nextType) flushList();
        listType = nextType;
        listItems.push(itemText);
        continue;
      }

      flushList();

      if (quoteMatch) {
        flushParagraph();
        quoteLines.push(quoteMatch[1]);
        continue;
      }

      flushQuote();

      if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
        flushParagraph();
        flushQuote();
        html.push('<hr>');
        continue;
      }

      paragraph.push(line);
    }

    flushParagraph();
    flushList();
    flushQuote();
    flushCodeBlock();
    return html.join('') || '<p></p>';
  };

  const renderReleaseNotes = (source) => {
    if (!source.trim()) return `<p>${escapeHtml(text('header.versionNotesEmpty', 'No release notes available.'))}</p>`;
    if (window.marked && typeof window.marked.parse === 'function') {
      return sanitizeRenderedHtml(window.marked.parse(source, {
        async: false,
        breaks: false,
        gfm: true,
      }));
    }
    return renderMarkdown(source);
  };

  const ensureVersionModal = () => {
    let overlay = document.getElementById('admin-version-modal');
    if (overlay) return overlay;

    overlay = document.createElement('div');
    overlay.id = 'admin-version-modal';
    overlay.className = 'modal-overlay';
        overlay.innerHTML = `
      <div class="modal admin-version-modal" role="dialog" aria-modal="true" aria-labelledby="admin-version-modal-title">
        <div class="admin-version-modal-head">
          <div class="modal-title" id="admin-version-modal-title"></div>
          <button type="button" class="admin-version-modal-close-x" id="admin-version-modal-close" aria-label="Close">✕</button>
        </div>
        <div class="admin-version-modal-body">
          <section class="admin-version-card">
            <div class="admin-version-card-hd">
              <div class="admin-version-card-title" id="admin-version-card-title">版本信息</div>
              <button id="admin-version-modal-refresh" type="button" class="btn btn-ghost btn-sm"></button>
            </div>
            <div class="admin-version-grid">
              <div class="admin-version-kv">
                <div class="admin-version-k" id="admin-version-modal-current-label"></div>
                <div class="admin-version-v" id="admin-version-modal-current"></div>
              </div>
              <div class="admin-version-kv">
                <div class="admin-version-k" id="admin-version-modal-latest-label"></div>
                <div class="admin-version-v" id="admin-version-modal-latest"></div>
              </div>
              <div class="admin-version-kv">
                <div class="admin-version-k" id="admin-version-modal-build-label">构建类型</div>
                <div class="admin-version-v" id="admin-version-modal-build">source / git</div>
              </div>
              <div class="admin-version-kv">
                <div class="admin-version-k" id="admin-version-modal-published-label"></div>
                <div class="admin-version-v" id="admin-version-modal-published"></div>
              </div>
            </div>
            <div style="margin-top:12px;display:flex;flex-wrap:wrap;gap:8px;align-items:center">
              <div class="admin-version-modal-badge" id="admin-version-modal-badge" hidden></div>
              <a id="admin-version-modal-link" class="btn btn-ghost btn-sm admin-version-modal-link" href="#" target="_blank" rel="noopener" hidden></a>
            </div>
            <div class="admin-version-modal-status is-hidden" id="admin-version-modal-status" style="margin-top:10px"></div>
          </section>
          <section class="admin-version-notes-wrap">
            <button type="button" class="admin-version-notes-toggle" id="admin-version-notes-toggle">
              <span id="admin-version-notes-toggle-text">更新内容</span>
              <span aria-hidden="true">▾</span>
            </button>
            <div class="admin-version-modal-notes" id="admin-version-modal-notes"></div>
          </section>
        </div>
        <div class="admin-version-modal-apply-msg" id="admin-version-modal-apply-msg" hidden></div>
        <div class="admin-version-modal-footer modal-footer">
          <button id="admin-version-modal-apply" type="button" class="btn btn-primary" hidden></button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);

    const close = () => {
      overlay.classList.remove('open');
      overlay.setAttribute('aria-hidden', 'true');
    };

    overlay.addEventListener('click', (event) => {
      if (event.target === overlay) close();
    });

    overlay.querySelector('#admin-version-modal-close')?.addEventListener('click', close);
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && overlay.classList.contains('open')) close();
    });
    overlay.querySelector('#admin-version-notes-toggle')?.addEventListener('click', () => {
      const notes = overlay.querySelector('#admin-version-modal-notes');
      notes?.classList.toggle('is-collapsed');
    });

    return overlay;
  };

  const renderVersionModal = (overlay = ensureVersionModal()) => {
    const title = overlay.querySelector('#admin-version-modal-title');
    const badge = overlay.querySelector('#admin-version-modal-badge');
    const status = overlay.querySelector('#admin-version-modal-status');
    const currentLabel = overlay.querySelector('#admin-version-modal-current-label');
    const currentValue = overlay.querySelector('#admin-version-modal-current');
    const latestLabel = overlay.querySelector('#admin-version-modal-latest-label');
    const latestValue = overlay.querySelector('#admin-version-modal-latest');
    const publishedLabel = overlay.querySelector('#admin-version-modal-published-label');
    const publishedValue = overlay.querySelector('#admin-version-modal-published');
    const notes = overlay.querySelector('#admin-version-modal-notes');
    const link = overlay.querySelector('#admin-version-modal-link');
    const refresh = overlay.querySelector('#admin-version-modal-refresh');
    const close = overlay.querySelector('#admin-version-modal-close');
    const latestVersion = String(updateInfo?.latest_version || appVersion || '').trim();
    const currentVersion = String(appVersion || updateInfo?.current_version || '').trim();
    const releaseUrl = String(updateInfo?.release_url || '').trim();
    const releaseNotes = String(updateInfo?.release_notes || '').trim();

    if (title) title.textContent = text('header.versionDialogTitle', 'Version');
    if (currentLabel) currentLabel.textContent = text('header.versionCurrent', 'Current');
    if (currentValue) currentValue.textContent = currentVersion ? `v${currentVersion.replace(/^v/i,'')}` : '-';
    if (latestLabel) latestLabel.textContent = text('header.versionLatest', 'Latest');
    if (latestValue) latestValue.textContent = latestVersion ? `v${latestVersion.replace(/^v/i,'')}` : '-';
    if (publishedLabel) publishedLabel.textContent = text('header.versionPublishedAt', 'Published');
    if (publishedValue) publishedValue.textContent = formatDateTime(updateInfo?.published_at);

    if (badge) {
      badge.hidden = true;
      badge.textContent = '';
      badge.className = 'admin-version-modal-badge';
    }

    if (status) {
      status.textContent = '';
      status.className = 'admin-version-modal-status is-hidden';
    }

    const cardTitle = overlay.querySelector('#admin-version-card-title');
    if (cardTitle) cardTitle.textContent = text('header.versionInfoSection', '版本信息');
    const buildLabel = overlay.querySelector('#admin-version-modal-build-label');
    if (buildLabel) buildLabel.textContent = text('header.versionBuildType', '构建类型');
    const notesToggleText = overlay.querySelector('#admin-version-notes-toggle-text');
    if (notesToggleText) {
      const ver = latestVersion ? `v${latestVersion.replace(/^v/i,'')}` : '';
      notesToggleText.textContent = ver
        ? text('header.versionNotesFor', '查看 {version} 更新内容', { version: ver })
        : text('header.versionNotes', '更新内容');
    }

    if (status) {
      status.textContent = '';
      status.className = 'admin-version-modal-status is-hidden';
    }
    if (badge) {
      badge.hidden = true;
      badge.textContent = '';
      badge.className = 'admin-version-modal-badge';
    }
    if (updateStatus === 'loading') {
      if (badge) {
        badge.hidden = false;
        badge.textContent = text('header.versionChecking', 'Checking for updates...');
        badge.className = 'admin-version-modal-badge is-muted';
      }
    } else if (updateStatus === 'error' || !updateInfo || updateInfo.status === 'error') {
      if (status) {
        const err = String(updateInfo?.error || text('header.versionUnavailable', 'Unable to check for updates right now.'));
        status.hidden = false;
        status.textContent = `${text('header.versionCheckFailed', '检查更新失败')}：${err}`;
        status.className = 'admin-version-modal-status is-error';
      }
    } else if (updateInfo.update_available) {
      if (badge) {
        badge.hidden = false;
        badge.textContent = text('header.versionUpdateAvailable', 'A new version is available.');
        badge.className = 'admin-version-modal-badge is-update';
      }
    } else {
      if (badge) {
        badge.hidden = false;
        badge.textContent = text('header.versionUpToDate', 'You are already on the latest version.');
        badge.className = 'admin-version-modal-badge is-current';
      }
    }

    if (notes) {
      if (updateStatus === 'loading') {
        notes.hidden = false;
        notes.innerHTML = `<p>${text('header.versionChecking', 'Checking for updates...')}</p>`;
      } else if (updateStatus === 'error' || !updateInfo || updateInfo.status === 'error') {
        notes.hidden = false;
        notes.innerHTML = `<p>${text('header.versionNotesEmpty', 'No release notes available.')}</p>`;
      } else {
        notes.hidden = false;
        notes.innerHTML = renderReleaseNotes(releaseNotes);
      }
    }

    if (link instanceof HTMLAnchorElement) {
      if (releaseUrl) {
        link.hidden = false;
        link.style.display = 'inline-flex';
        link.href = releaseUrl;
        link.textContent = text('header.versionOpenRelease', 'Open Release');
      } else {
        link.hidden = true;
        link.style.display = 'none';
        link.removeAttribute('href');
        link.textContent = '';
      }
    }

    if (refresh instanceof HTMLButtonElement) {
      refresh.textContent = text('header.versionRefresh', 'Check Now');
      refresh.disabled = updateStatus === 'loading' || applyStatus === 'loading';
    }

    const apply = overlay.querySelector('#admin-version-modal-apply');
    const applyMsg = overlay.querySelector('#admin-version-modal-apply-msg');
    const canOfferApply = updateStatus === 'ready'
      && updateInfo
      && updateInfo.status !== 'error'
      && Boolean(updateInfo.update_available);
    if (apply instanceof HTMLButtonElement) {
      apply.hidden = !canOfferApply && applyStatus === 'idle';
      apply.textContent = applyStatus === 'loading'
        ? text('header.versionApplying', 'Updating safely...')
        : text('header.versionApply', 'Safe Update');
      apply.disabled = applyStatus === 'loading' || updateStatus === 'loading' || !canOfferApply;
      // keep visible after an attempt so user can re-read result context
      if (applyStatus !== 'idle') apply.hidden = false;
    }
    if (applyMsg instanceof HTMLElement) {
      let message = '';
      let kind = 'muted';
      if (applyStatus === 'loading') {
        message = text('header.versionApplying', 'Updating safely...');
      } else if (applyResult) {
        const reason = String(applyResult.reason || '');
        if (applyResult.applied) {
          if (applyResult.restart_scheduled) {
            message = text('header.versionApplyRestarting', 'Merged safely. Service is restarting to load the new code.');
          } else if (applyResult.needs_restart) {
            message = text('header.versionApplyNeedRestart', 'Merge succeeded. Restart the service to fully apply it.');
          } else {
            message = text('header.versionApplyOk', 'Merged upstream safely.');
          }
          kind = 'ok';
        } else if (reason === 'already_up_to_date') {
          message = text('header.versionApplyUpToDate', 'Already up to date. Nothing to merge.');
          kind = 'ok';
        } else if (reason === 'dirty_worktree') {
          message = text('header.versionApplyBlockedDirty', 'Local uncommitted changes detected. Auto-update refused to avoid overwrite.');
          kind = 'error';
        } else if (reason === 'merge_conflict' || reason === 'merge_failed' || applyResult.status === 'conflict') {
          const files = Array.isArray(applyResult.conflict_files) ? applyResult.conflict_files : [];
          const report = applyResult.conflict_report || applyResult.log_path || '';
          message = text('header.versionApplyConflict', 'Merge conflict detected. Aborted. Check the log and ask an agent to help merge.');
          if (files.length) message += ` (${files.slice(0, 8).join(', ')}${files.length > 8 ? '…' : ''})`;
          if (report) message += ` | ${text('header.versionConflictReport', 'Conflict report')}: ${report}`;
          kind = 'error';
        } else {
          message = applyResult.message || text('header.versionApplyFailed', 'Safe update failed.');
          if (applyResult.log_path) message += ` | log: ${applyResult.log_path}`;
          kind = 'error';
        }
      }
      if (message) {
        applyMsg.hidden = false;
        applyMsg.dataset.kind = kind;
        applyMsg.textContent = message;
      } else {
        applyMsg.hidden = true;
        applyMsg.textContent = '';
        delete applyMsg.dataset.kind;
      }
    }

    if (close instanceof HTMLButtonElement) {
      close.textContent = text('header.versionClose', 'Close');
      close.disabled = applyStatus === 'loading';
    }

    overlay.classList.add('open');
    overlay.setAttribute('aria-hidden', 'false');
  };


  const applyUpdate = async () => {
    if (applyStatus === 'loading') return;
    applyStatus = 'loading';
    applyResult = null;
    const overlay = ensureVersionModal();
    renderVersionModal(overlay);
    try {
      const key = await adminKey.get();
      const response = await fetch(`${ADMIN_API}/update/apply`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${key || ''}`,
          Accept: 'application/json',
        },
        cache: 'no-store',
      });
      if (response.status === 401 || response.status === 403) {
        adminLogout();
        return;
      }
      const data = await response.json().catch(() => ({}));
      applyResult = data && typeof data === 'object' ? data : { status: 'error', message: 'invalid response' };
      if (!response.ok && !applyResult.status) {
        applyResult = { status: 'error', message: `HTTP ${response.status}` };
      }
      applyStatus = applyResult.status === 'ok' && (applyResult.applied || applyResult.reason === 'already_up_to_date')
        ? 'ok'
        : (applyResult.status === 'conflict' || applyResult.status === 'blocked' || applyResult.status === 'error')
          ? 'error'
          : (applyResult.applied ? 'ok' : 'error');
      if (applyResult.applied) {
        // 合并成功后服务可能即将重启；先更新文案，再等待健康检查恢复
        if (applyResult.restart_scheduled) {
          const waitRestart = async () => {
            const started = Date.now();
            // 给后端一点时间挂掉再起来
            await new Promise((r) => setTimeout(r, 1500));
            while (Date.now() - started < 45000) {
              try {
                const metaRes = await fetch('/meta', { cache: 'no-store' });
                if (metaRes.ok) {
                  const meta = await metaRes.json();
                  appVersion = String(meta?.version || appVersion || '').trim();
                  window.__grok2apiMetaVersion = appVersion;
                  await refreshUpdate(true);
                  return;
                }
              } catch {}
              await new Promise((r) => setTimeout(r, 1000));
            }
          };
          waitRestart().finally(() => {
            applyVersion();
            const ov = ensureVersionModal();
            if (ov.classList.contains('open')) renderVersionModal(ov);
          });
        } else {
          try {
            const metaRes = await fetch('/meta', { cache: 'no-store' });
            if (metaRes.ok) {
              const meta = await metaRes.json();
              appVersion = String(meta?.version || appVersion || '').trim();
              window.__grok2apiMetaVersion = appVersion;
            }
          } catch {}
          await refreshUpdate(true);
        }
      }
    } catch (error) {
      applyStatus = 'error';
      applyResult = {
        status: 'error',
        reason: 'request_failed',
        message: error?.message || String(error),
      };
    } finally {
      applyVersion();
      if (overlay.classList.contains('open')) {
        renderVersionModal(overlay);
      }
    }
  };

  const openVersionModal = async () => {
    const overlay = ensureVersionModal();
    applyStatus = 'idle';
    applyResult = null;
    updateStatus = 'loading';
    renderVersionModal(overlay);
    try {
      // 每次点开版本都强制检查一次，参考「立即检查」体验
      await refreshUpdate(true);
    } finally {
      applyVersion();
      if (overlay.classList.contains('open')) {
        renderVersionModal(overlay);
      }
    }
  };

  const applyVersion = () => {
    const node = mount.querySelector('#hd-version');
    if (!(node instanceof HTMLElement)) return;
    const label = appVersion ? `v${appVersion.replace(/^v/i, '')}` : '—';
    node.textContent = label;
    node.title = appVersion ? `Grok2API ${label}` : 'Version';
    node.setAttribute('aria-label', node.title);
    node.classList.toggle('has-update', Boolean(updateInfo?.update_available));
  };

  await loadVersion();

  const headerLooksValid = (html) => typeof html === 'string'
    && html.includes('id="hd-version"')
    && !html.includes('id="hd-version-tag"')
    && !html.includes('v0.2.2');

  try {
    let html = '';
    const memoryHtml = window.__grok2apiAdminHeaderHtml || '';
    const cachedHtml = readSessionCache(HEADER_HTML_CACHE_KEY);
    if (headerLooksValid(memoryHtml)) html = memoryHtml;
    else if (headerLooksValid(cachedHtml)) html = cachedHtml;

    if (!html) {
      const res = await fetch(`/static/admin/header.html?v=${encodeURIComponent(scriptVersion)}&r=${headerCacheRev}`, { cache: 'no-store' });
      if (!res.ok) throw new Error('header unavailable');
      html = await res.text();
    }

    if (!headerLooksValid(html)) throw new Error('header markup outdated');
    mount.innerHTML = html;
    window.__grok2apiAdminHeaderHtml = html;
    writeSessionCache(HEADER_HTML_CACHE_KEY, html);
  } catch {
    mount.innerHTML = `
      <header class="admin-header">
        <div class="admin-header-inner">
          <div class="admin-brand-wrap">
            <a href="https://github.com/jiujiu532/grok2api" target="_blank" rel="noopener" class="admin-brand-link">
              <span class="admin-brand-mark" aria-hidden="true">G</span>
              <span class="admin-brand">Grok2API</span>
            </a>
            <a href="https://blog.cheny.me/" target="_blank" rel="noopener" class="admin-username" id="hd-user">@Chenyme</a>
            <a href="https://github.com/jiujiu532" target="_blank" rel="noopener" class="admin-username">@jiu</a>
          </div>
          <nav class="admin-nav">
            <a href="/admin/dashboard" class="admin-nav-link" data-nav="/admin/dashboard" data-i18n="header.dashboard">仪表盘</a>
            <a href="/admin/account" class="admin-nav-link" data-nav="/admin/account" data-i18n="header.account">账户管理</a>
            <a href="/admin/config" class="admin-nav-link" data-nav="/admin/config" data-i18n="header.config">配置管理</a>
            <a href="/admin/cache" class="admin-nav-link" data-nav="/admin/cache" data-i18n="header.cache">缓存管理</a>
          </nav>
          <div class="admin-header-right">
            <button type="button" class="admin-header-version" id="hd-version" aria-label="Version">—</button>
            <div class="admin-lang-menu" id="hd-lang-menu">
              <button type="button" class="btn admin-header-control admin-lang-trigger" id="hd-lang-trigger" aria-label="Language" aria-haspopup="menu" aria-expanded="false">
                <span class="admin-lang-trigger-code" id="hd-lang-code">CN</span>
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="m7 10 5 5 5-5"/>
                </svg>
              </button>
              <div class="admin-lang-popover" id="hd-lang-popover" role="menu" aria-labelledby="hd-lang-trigger">
                <button type="button" class="admin-lang-option" data-lang="zh" role="menuitem">
                  <span class="admin-lang-option-code">CN</span>
                  <span class="admin-lang-option-name">简体中文</span>
                </button>
                <button type="button" class="admin-lang-option" data-lang="en" role="menuitem">
                  <span class="admin-lang-option-code">EN</span>
                  <span class="admin-lang-option-name">English</span>
                </button>
                <button type="button" class="admin-lang-option" data-lang="ja" role="menuitem">
                  <span class="admin-lang-option-code">JA</span>
                  <span class="admin-lang-option-name">日本語</span>
                </button>
                <button type="button" class="admin-lang-option" data-lang="es" role="menuitem">
                  <span class="admin-lang-option-code">ES</span>
                  <span class="admin-lang-option-name">Español</span>
                </button>
                <button type="button" class="admin-lang-option" data-lang="de" role="menuitem">
                  <span class="admin-lang-option-code">DE</span>
                  <span class="admin-lang-option-name">Deutsch</span>
                </button>
                <button type="button" class="admin-lang-option" data-lang="fr" role="menuitem">
                  <span class="admin-lang-option-code">FR</span>
                  <span class="admin-lang-option-name">Français</span>
                </button>
              </div>
            </div>
            <button onclick="adminLogout()" class="btn admin-header-control admin-header-icon-btn" id="hd-logout" aria-label="Logout" title="Logout">
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
                <path d="M16 17l5-5-5-5"/>
                <path d="M21 12H9"/>
              </svg>
            </button>
          </div>
        </div>
      </header>`;
  }

  // 兜底：旧缓存若残留不可点的 version tag，就地替换成可点按钮
  const staleTag = mount.querySelector('#hd-version-tag');
  if (staleTag && !mount.querySelector('#hd-version')) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'admin-header-version';
    btn.id = 'hd-version';
    btn.setAttribute('aria-label', 'Version');
    btn.textContent = '—';
    staleTag.replaceWith(btn);
  }

  const active = mount.dataset.active || location.pathname;
  mount.querySelectorAll('[data-nav]').forEach((link) => {
    link.classList.toggle('active', link.dataset.nav === active);
  });

  const syncLanguageMenu = initLanguageMenu();
  applyHeaderI18n();
  applyVersion();
  syncLanguageMenu?.();

  const bindVersionClick = () => {
    const node = mount.querySelector('#hd-version');
    if (!(node instanceof HTMLElement) || node.dataset.boundVersionClick === '1') return;
    node.dataset.boundVersionClick = '1';
    node.style.pointerEvents = 'auto';
    node.style.cursor = 'pointer';
    node.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      openVersionModal();
    });
  };
  bindVersionClick();

  // 后台静默检查更新，避免版本徽标永远停在“无更新”灰色态
  refreshUpdate(false).then(() => {
    applyVersion();
  }).catch(() => {});

  const versionModal = ensureVersionModal();
  versionModal.querySelector('#admin-version-modal-refresh')?.addEventListener('click', async () => {
    applyStatus = 'idle';
    applyResult = null;
    updateStatus = 'loading';
    renderVersionModal(versionModal);
    try {
      await refreshUpdate(true);
    } finally {
      applyVersion();
      if (versionModal.classList.contains('open')) {
        renderVersionModal(versionModal);
      }
    }
  });
  versionModal.querySelector('#admin-version-modal-apply')?.addEventListener('click', () => {
    applyUpdate();
  });
};
