/* ============================================================================
   媒体音频提取器 —— 前端逻辑
   ----------------------------------------------------------------------------
   依赖 ./app.js 提供的 API / U / UI / Jobs / Bars / Shell 与 ./icons.js 的 Icons。
   所有请求走 API.get/post（它自动处理平台前缀与鉴权头）。
   ========================================================================== */

(function () {
  'use strict';

  const State = {
    engines: null,
    presets: [],
    summary: null,
    allowedRoots: [],
    extract: { inputs: [], output: '', track: 'auto', preset: 'none' },
    library: { files: [], total: 0, query: '', offset: 0 },
    selected: new Set(),
  };

  /* ------------------------------------------------------------ 概览 */

  async function renderOverview(host) {
    host.innerHTML = '';
    host.appendChild(UI.banner('info', T('正在加载…'), ''));

    let summary = { total: 0, with_audio: 0, failed: 0, total_bytes: 0, families: [],
                    extracted: 0, extracted_bytes: 0 };
    try {
      const [summaryData, filesData] = await Promise.all([
        API.get('api/media/summary'),
        API.get('api/media/results?limit=8'),
      ]);
      summary = summaryData;
      State.summary = summaryData;
      State.recent = filesData.results || [];
    } catch (error) {
      host.innerHTML = '';
      host.appendChild(UI.banner('error', T('无法读取统计信息'), U.esc(error.message) +
        T('<ul><li>服务可能正在重启，稍后重试</li><li>或查看日志：journalctl -u shh11-media-audio</li></ul>')));
      return;
    }

    host.innerHTML = '';

    if (!State.allowedRoots.length) {
      const action = U.el('button', { class: 'btn primary', text: T('去设置可访问目录') });
      action.addEventListener('click', () => Shell.show('settings'));
      const banner = UI.banner('warn', T('还没有配置可访问目录'),
        T('本应用默认只读、且白名单为空 —— 必须由你指定它才能读哪些目录。'));
      banner.querySelector('.bd').appendChild(U.el('div', { class: 'mt1' }, [action]));
      host.appendChild(banner);
    }

    const tiles = U.el('div', { class: 'grid cols-4 mb2' }, [
      tile(T('已收录文件'), U.num(summary.total), U.size(summary.total_bytes)),
      tile(T('含音轨'), U.num(summary.with_audio), summary.total ? U.pct(summary.with_audio / summary.total * 100) : '—'),
      tile(T('已提取'), U.num(summary.extracted), U.size(summary.extracted_bytes)),
      tile(T('解析失败'), U.num(summary.failed), summary.failed ? T('查看媒体库') : T('无')),
    ]);
    host.appendChild(tiles);

    const engineCard = U.el('div', { class: 'card' }, [
      U.el('h2', {}, [
        U.el('span', { html: Icons.svg('cpu', { size: 17 }) }),
        U.el('span', { text: T('处理能力') }),
      ]),
    ]);
    const detail = (State.engines && State.engines.engine_detail) || {};
    if (detail.available) {
      engineCard.appendChild(UI.banner('ok', T('无损抽取 + 转码都可用'),
        T('核心的无损抽取由本应用自行实现，不依赖任何外部程序；<br>') +
        T('系统上还检测到 ffmpeg，因此额外提供了 MP3 / FLAC / WAV / M4A / OPUS 转码预设。')));
    } else {
      engineCard.appendChild(UI.banner('info', T('无损抽取可用（完全离线）'),
        T('无损抽取（把原音轨原样搬到 M4A / MKA 里，不重编码）由本应用自行实现，') +
        T('不需要任何外部程序。<br>') +
        T('转码预设需要系统上有 ffmpeg 命令，当前未检测到 —— ') + U.esc(detail.detail || '') +
        T('<br>这不影响正常使用。')));
    }
    host.appendChild(engineCard);

    if (summary.families && summary.families.length) {
      const card = U.el('div', { class: 'card' }, [
        U.el('h2', {}, [
          U.el('span', { html: Icons.svg('chart', { size: 17 }) }),
          U.el('span', { text: T('文件类型分布') }),
        ]),
      ]);
      const bars = U.el('div', {});
      card.appendChild(bars);
      host.appendChild(card);
      const label = { 'iso-bmff': 'MP4 / MOV / M4A（ISO-BMFF）', ebml: 'MKV / WebM（EBML）',
                      'raw-audio': T('纯音频（WAV / FLAC / MP3 / OGG）') };
      Bars.render(bars, summary.families.map((row, index) => ({
        label: label[row.family] || row.family || T('未分类'),
        value: row.bytes,
        text: U.num(row.n) + T(' 个 · ') + U.size(row.bytes),
        color: U.color(index, 48),
      })));
    }

    if ((State.recent || []).length) {
      const card = U.el('div', { class: 'card flush' }, [
        U.el('div', { class: 'card-head' }, [U.el('h2', { class: 'mb0' }, [
          U.el('span', { html: Icons.svg('clock', { size: 17 }) }),
          U.el('span', { text: T('最近提取') }),
        ])]),
      ]);
      const body = U.el('div', { class: 'card-body' });
      const table = U.el('table', { class: 'data' }, [
        U.el('thead', {}, [U.el('tr', {}, [
          U.el('th', { text: T('源文件') }), U.el('th', { text: T('输出') }),
          U.el('th', { text: T('方式') }), U.el('th', { class: 'num', text: T('大小') }),
          U.el('th', { text: T('时间') }),
        ])]),
      ]);
      const tbody = U.el('tbody', {});
      State.recent.forEach((row) => {
        tbody.appendChild(U.el('tr', {}, [
          U.el('td', { class: 'path-cell mono small', text: baseName(row.source) }),
          U.el('td', { class: 'path-cell mono small', text: row.output ? baseName(row.output) : '—' }),
          U.el('td', {}, [UI.badge(row.mode === 'stream-copy' ? T('无损复制') : (row.mode || '—'),
                                 row.mode === 'stream-copy' ? 'ok' : 'info')]),
          U.el('td', { class: 'num', text: U.size(row.bytes) }),
          U.el('td', { class: 'small nowrap', text: U.time(row.created_at) }),
        ]));
      });
      table.appendChild(tbody);
      body.appendChild(table);
      card.appendChild(body);
      host.appendChild(card);
    }
  }

  function tile(label, value, hint) {
    return U.el('div', { class: 'stat-tile' }, [
      U.el('div', { class: 'label', text: label }),
      U.el('div', { class: 'value' }, [
        U.el('span', { text: value }),
        hint ? U.el('small', { text: hint }) : null,
      ]),
    ]);
  }

  /* ------------------------------------------------------------ 提取 */

  async function renderExtract(host) {
    host.innerHTML = '';
    const card = U.el('div', { class: 'card' });
    card.appendChild(U.el('h2', {}, [
      U.el('span', { html: Icons.svg('scissors', { size: 17 }) }),
      U.el('span', { text: T('提取音轨') }),
    ]));
    card.appendChild(U.el('div', { class: 'card-hint',
      text: T('选择媒体文件或目录，把其中的音轨无损抽出来。源文件不会被修改。') }));

    // 输入
    const inputPath = U.el('div', { class: 'path empty', text: T('尚未选择') });
    const chips = U.el('div', { class: 'chips mt1' });
    const pickDirBtn = U.el('button', { class: 'btn' }, [
      U.el('span', { html: Icons.svg('folder', { size: 15 }) }), U.el('span', { text: T('添加目录') }),
    ]);
    const pickFileBtn = U.el('button', { class: 'btn' }, [
      U.el('span', { html: Icons.svg('film', { size: 15 }) }), U.el('span', { text: T('添加文件') }),
    ]);
    pickDirBtn.addEventListener('click', () => {
      UI.pickDir({
        title: T('选择要处理的目录'),
        start: State.extract.inputs[0] || '',
        onPick: (path) => { addInput(path); },
      });
    });
    pickFileBtn.addEventListener('click', () => {
      UI.pickDir({
        title: T('选择媒体文件'),
        pickFile: true,
        start: State.extract.inputs[0] || '',
        onPick: (path) => { addInput(path); },
      });
    });

    card.appendChild(U.el('label', { class: 'field' }, [
      U.el('span', { class: 'label-text', text: T('输入（目录或文件，可多个）') }),
      U.el('div', { class: 'picker-row' }, [inputPath, pickDirBtn, pickFileBtn]),
      chips,
    ]));

    function renderChips() {
      chips.innerHTML = '';
      if (!State.extract.inputs.length) return;
      State.extract.inputs.forEach((path) => {
        const button = U.el('button', { title: T('移除'), text: '×' });
        button.addEventListener('click', () => {
          State.extract.inputs = State.extract.inputs.filter((item) => item !== path);
          renderChips();
        });
        chips.appendChild(U.el('span', { class: 'chip' }, [
          U.el('span', { text: path, title: path }), button,
        ]));
      });
    }

    function addInput(path) {
      if (!path) return;
      if (State.extract.inputs.includes(path)) { UI.warn(T('已经添加过了')); return; }
      State.extract.inputs.push(path);
      inputPath.textContent = State.extract.inputs.length + T(' 项');
      inputPath.classList.remove('empty');
      renderChips();
      refreshSubmit();
    }
    if (State.extract.inputs.length) {
      inputPath.textContent = State.extract.inputs.length + T(' 项');
      inputPath.classList.remove('empty');
      renderChips();
    }

    // 输出
    const outputPath = U.el('div', {
      class: 'path' + (State.extract.output ? '' : ' empty'),
      text: State.extract.output || T('尚未选择（默认放到应用数据目录的 output/）'),
    });
    const pickOut = U.el('button', { class: 'btn' }, [
      U.el('span', { html: Icons.svg('folderOpen', { size: 15 }) }), U.el('span', { text: T('选择输出目录') }),
    ]);
    pickOut.addEventListener('click', () => {
      UI.pickDir({
        title: T('选择输出目录'),
        start: State.extract.output || '',
        onPick: (path) => {
          State.extract.output = path;
          outputPath.textContent = path;
          outputPath.classList.remove('empty');
          refreshSubmit();
        },
      });
    });
    card.appendChild(U.el('label', { class: 'field' }, [
      U.el('span', { class: 'label-text', text: T('输出目录') }),
      U.el('div', { class: 'picker-row' }, [outputPath, pickOut]),
      U.el('span', { class: 'help', text: T('不选则输出到应用自己的 output/ 目录；重名会自动加序号，不会覆盖已有文件。') }),
    ]));

    // 轨道选择
    const trackSelect = U.el('select', {}, [
      U.el('option', { value: 'auto', text: T('自动（第一条音轨）') }),
    ]);
    State.presets.forEach(() => {});
    [1, 2, 3, 4].forEach((n) => {
      trackSelect.appendChild(U.el('option', { value: String(n), text: T('轨道 ') + n }));
    });
    trackSelect.value = State.extract.track;
    trackSelect.addEventListener('change', () => { State.extract.track = trackSelect.value; });

    // 输出格式
    const presetSelect = U.el('select', {});
    presetSelect.appendChild(U.el('option', {
      value: 'none', text: T('无损抽取（流复制，不重编码）'),
    }));
    const ffmpegOk = !!(State.engines && State.engines.engine_detail
      && State.engines.engine_detail.available);
    State.presets.forEach((preset) => {
      const option = U.el('option', {
        value: preset.id,
        text: T('转码为 ') + preset.label + (ffmpegOk ? '' : T('（需 ffmpeg）')),
        disabled: !ffmpegOk,
      });
      presetSelect.appendChild(option);
    });
    presetSelect.value = State.extract.preset;
    presetSelect.addEventListener('change', () => { State.extract.preset = presetSelect.value; });

    card.appendChild(U.el('div', { class: 'row' }, [
      U.el('label', { class: 'field' }, [
        U.el('span', { class: 'label-text', text: T('音轨') }), trackSelect,
      ]),
      U.el('label', { class: 'field' }, [
        U.el('span', { class: 'label-text', text: T('输出格式') }), presetSelect,
      ]),
    ]));
    if (!ffmpegOk) {
      card.appendChild(UI.banner('info', T('当前只有「无损抽取」可用'),
        T('转码预设需要系统上安装 ffmpeg。无损抽取不重编码、速度更快、也没有质量损失，') +
        T('通常正是想要的结果。')));
    }

    const submit = U.el('button', { class: 'btn primary', disabled: true }, [
      U.el('span', { html: Icons.svg('play', { size: 15 }) }),
      U.el('span', { text: T('开始提取') }),
    ]);
    submit.addEventListener('click', async () => {
      const params = {
        roots: [], inputs: [], output_dir: State.extract.output || '',
        track_id: State.extract.track, preset: State.extract.preset,
      };
      // 目录与文件分开传：目录要递归走，文件就单个处理
      for (const path of State.extract.inputs) {
        if (path.endsWith('/') || !/\.[A-Za-z0-9]{2,5}$/.test(path)) params.roots.push(path);
        else params.inputs.push(path);
      }
      if (!params.roots.length && !params.inputs.length) {
        UI.warn(T('请先选择输入')); return;
      }
      submit.disabled = true;
      try {
        await Jobs.submit('extract', params, T('提取音轨（') + State.extract.inputs.length + T(' 项）'));
        Shell.show('jobs');
      } catch (error) {
        UI.err(error);
      } finally {
        submit.disabled = false;
        refreshSubmit();
      }
    });
    card.appendChild(U.el('div', { class: 'btn-row mt1' }, [submit]));

    function refreshSubmit() {
      submit.disabled = !State.extract.inputs.length;
    }
    refreshSubmit();

    host.appendChild(card);

    // 批量说明
    host.appendChild(U.el('div', { class: 'card' }, [
      U.el('h2', {}, [
        U.el('span', { html: Icons.svg('info', { size: 17 }) }),
        U.el('span', { text: T('输出说明') }),
      ]),
      U.el('div', { class: 'small muted prewrap', text:
        T('· MP4 / MOV / M4A 的音轨 → .m4a（容器内编码不变，例如 AAC 仍是 AAC）\n') +
        T('· MKV / WebM 的音轨 → .mka\n') +
        T('· 选择转码预设时 → 按预设扩展名输出（.mp3 / .flac / .wav / .m4a / .opus）\n') +
        T('· 重名不覆盖：自动追加 -1、-2 序号\n') +
        T('· 源文件全程只读，不会被修改或删除') }),
    ]));
  }

  /* ------------------------------------------------------------ 媒体库 */

  async function renderLibrary(host) {
    host.innerHTML = '';
    const card = U.el('div', { class: 'card flush' });

    const search = U.el('input', { type: 'search', placeholder: T('按路径搜索…'),
                                   value: State.library.query });
    const scanBtn = U.el('button', { class: 'btn primary' }, [
      U.el('span', { html: Icons.svg('scan', { size: 15 }) }),
      U.el('span', { text: T('扫描目录') }),
    ]);
    scanBtn.addEventListener('click', async () => {
      if (!State.extract.inputs.length) {
        UI.pickDir({
          title: T('选择要扫描的目录'),
          onPick: async (path) => {
            try {
              await Jobs.submit('scan', { roots: [path] }, T('扫描 ') + baseName(path));
              Shell.show('jobs');
            } catch (error) { UI.err(error); }
          },
        });
        return;
      }
      try {
        await Jobs.submit('scan', { roots: State.extract.inputs }, T('扫描媒体文件'));
        Shell.show('jobs');
      } catch (error) { UI.err(error); }
    });

    const refreshBtn = U.el('button', { class: 'btn' }, [
      U.el('span', { html: Icons.svg('refresh', { size: 15 }) }),
      U.el('span', { text: T('刷新') }),
    ]);
    refreshBtn.addEventListener('click', () => loadLibrary(host));

    card.appendChild(U.el('div', { class: 'card-head spread' }, [
      U.el('h2', { class: 'mb0' }, [
        U.el('span', { html: Icons.svg('list', { size: 17 }) }),
        U.el('span', { text: T('媒体库') }),
      ]),
      U.el('div', { class: 'btn-row' }, [search, refreshBtn, scanBtn]),
    ]));

    const body = U.el('div', { class: 'card-body' });
    card.appendChild(body);
    host.appendChild(card);

    const selectedBar = U.el('div', { class: 'card' });
    host.appendChild(selectedBar);
    renderSelectedBar(selectedBar);

    search.addEventListener('input', U.debounce(() => {
      State.library.query = search.value.trim();
      loadLibrary(host);
    }, 300));

    await loadLibrary(host, body, selectedBar);
  }

  function renderSelectedBar(host) {
    host.innerHTML = '';
    if (!State.selected.size) {
      host.appendChild(U.el('div', { class: 'small muted',
        text: T('提示：扫描后可以勾选文件，然后批量提取。') }));
      return;
    }
    const extractBtn = U.el('button', { class: 'btn primary' }, [
      U.el('span', { html: Icons.svg('scissors', { size: 15 }) }),
      U.el('span', { text: T('提取选中（') + State.selected.size + '）' }),
    ]);
    extractBtn.addEventListener('click', async () => {
      const outDir = State.extract.output;
      if (!outDir) {
        UI.pickDir({
          title: T('选择输出目录'),
          onPick: async (path) => {
            State.extract.output = path;
            await submitSelected();
          },
        });
        return;
      }
      await submitSelected();
    });
    async function submitSelected() {
      try {
        await Jobs.submit('extract', {
          inputs: Array.from(State.selected), output_dir: State.extract.output,
          track_id: State.extract.track, preset: State.extract.preset,
        }, T('提取选中文件（') + State.selected.size + '）');
        Shell.show('jobs');
      } catch (error) { UI.err(error); }
    }
    const clear = U.el('button', { class: 'btn ghost', text: T('清空选择') });
    clear.addEventListener('click', () => { State.selected.clear(); Shell.show('library'); });

    host.appendChild(U.el('div', { class: 'btn-row' }, [
      U.el('span', { class: 'small muted', text: T('已选 ') + State.selected.size + T(' 个文件') }),
      extractBtn, clear,
    ]));
  }

  async function loadLibrary(host, body, selectedBar) {
    if (!body) {
      body = host.querySelector('.card-body');
    }
    body.innerHTML = '';
    body.appendChild(U.el('div', { class: 'empty' }, [U.el('div', { class: 'ed', text: T('加载中…') })]));

    let data;
    try {
      data = await API.get('api/media/files?limit=200&q=' + encodeURIComponent(State.library.query)
        + '&with_audio=true');
    } catch (error) {
      body.innerHTML = '';
      body.appendChild(UI.banner('error', T('无法读取媒体库'), U.esc(error.message)));
      return;
    }
    State.library.files = data.files || [];
    State.library.total = data.total || 0;

    body.innerHTML = '';
    if (!State.library.files.length) {
      body.appendChild(UI.empty('film', T('媒体库里还没有内容'),
        State.library.query ? T('没有匹配「') + U.esc(State.library.query) + T('」的文件')
                            : T('点右上角「扫描目录」，先让应用读一遍你的媒体文件。')));
      return;
    }

    const headCheck = U.el('input', { type: 'checkbox' });
    headCheck.addEventListener('change', () => {
      State.library.files.forEach((file) => {
        if (headCheck.checked) State.selected.add(file.path);
        else State.selected.delete(file.path);
      });
      loadLibrary(host, body, selectedBar);
    });

    const table = U.el('table', { class: 'data' }, [
      U.el('thead', {}, [U.el('tr', {}, [
        U.el('th', {}, [headCheck]),
        U.el('th', { text: T('文件') }),
        U.el('th', { text: T('音轨') }),
        U.el('th', { text: T('时长') }),
        U.el('th', { class: 'num', text: T('大小') }),
        U.el('th', { text: '' }),
      ])]),
    ]);
    const tbody = U.el('tbody', {});
    State.library.files.forEach((file) => {
      const track = (file.audio_tracks || [])[0];
      const check = U.el('input', { type: 'checkbox', checked: State.selected.has(file.path) });
      check.addEventListener('change', () => {
        if (check.checked) State.selected.add(file.path);
        else State.selected.delete(file.path);
        renderSelectedBar(selectedBar);
      });

      const info = U.el('button', { class: 'btn sm ghost', title: T('详情') },
        [U.el('span', { html: Icons.svg('eye', { size: 13 }) })]);
      info.addEventListener('click', () => showFileDetail(file));

      tbody.appendChild(U.el('tr', { class: State.selected.has(file.path) ? 'selected' : '' }, [
        U.el('td', {}, [check]),
        U.el('td', { class: 'path-cell' }, [
          U.el('div', { text: baseName(file.path) }),
          U.el('div', { class: 'small faint mono', text: dirName(file.path) }),
        ]),
        U.el('td', {}, track
          ? [UI.badge(track.codec_name || track.codec || T('未知'), 'info')]
          : [UI.badge(file.error ? T('解析失败') : T('无音轨'), file.error ? 'danger' : 'neutral')]),
        U.el('td', { class: 'small nowrap', text: file.duration ? fmtDuration(file.duration) : '—' }),
        U.el('td', { class: 'num', text: U.size(file.size) }),
        U.el('td', {}, [info]),
      ]));
    });
    table.appendChild(tbody);
    body.appendChild(table);
    body.appendChild(U.el('div', { class: 'small faint mt1',
      text: T('共 ') + U.num(State.library.total) + T(' 个含音轨的文件，显示前 ')
            + State.library.files.length + T(' 个') }));
  }

  function showFileDetail(file) {
    const rows = (file.audio_tracks || []).map((track) => `
      <tr><td>${U.esc(String(track.track_id))}</td>
          <td>${U.esc(track.codec_name || track.codec || '—')}</td>
          <td>${U.esc(track.language || '—')}</td>
          <td>${track.channels || '—'}</td>
          <td>${track.sample_rate ? U.num(track.sample_rate) + ' Hz' : '—'}</td>
          <td>${track.extractable ? '可无损抽取' : '仅可转码'}</td></tr>`).join('');
    const html = `
      <div class="small mono prewrap mb2">${U.esc(file.path)}</div>
      ${file.error ? `<div class="banner error"><div><div class="bt">解析失败</div>
        <div class="bd">${U.esc(file.error)}</div></div></div>` : ''}
      <table class="data">
        <thead><tr><th>${T('轨道')}</th><th>${T('编码')}</th><th>${T('语言')}</th><th>${T('声道')}</th><th>${T('采样率')}</th><th>${T('抽取')}</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="6" class="muted">没有音轨</td></tr>'}</tbody>
      </table>
      <div class="small muted mt1">${T('容器：')}${U.esc(file.family || '—')} ${T('· 大小')} ${U.size(file.size)}
        ${file.duration ? ' · 时长 ' + fmtDuration(file.duration) : ''}</div>`;
    UI.modal({ title: T('文件详情'), icon: 'film', wide: true, bodyHtml: html,
               buttons: [{ text: T('关闭') }] });
  }

  /* ------------------------------------------------------------ 任务 */

  async function renderJobs(host) {
    host.innerHTML = '';
    const card = U.el('div', { class: 'card flush' });
    card.appendChild(U.el('div', { class: 'card-head spread' }, [
      U.el('h2', { class: 'mb0' }, [
        U.el('span', { html: Icons.svg('activity', { size: 17 }) }),
        U.el('span', { text: T('任务') }),
      ]),
      U.el('div', { class: 'btn-row' }, [
        U.el('span', { class: 'small muted', id: 'job-counts' }),
      ]),
    ]));
    const body = U.el('div', { class: 'card-body' });
    card.appendChild(body);
    host.appendChild(card);

    let data;
    try {
      data = await API.get('api/jobs?limit=100');
    } catch (error) {
      body.appendChild(UI.banner('error', T('无法读取任务列表'), U.esc(error.message)));
      return;
    }
    const counts = data.counts || {};
    U.byId('job-counts').textContent =
      `${T('运行中')} ${counts.running || 0} ${T('· 排队')} ${counts.queued || 0} ${T('· 已完成')} ${counts.completed || 0} ${T('· 失败')} ${counts.failed || 0}`;

    Jobs.reload = () => Shell.show('jobs');
    Jobs.renderTable(body, data.jobs || [], { emptyHint: T('还没有任务') });
  }

  /* ------------------------------------------------------------ 设置 */

  async function renderSettings(host) {
    host.innerHTML = '';
    // 界面语言（放最前：非中文用户进来第一眼就该看到它）
    // UI.langSelect() 内部已处理「落 localStorage + 套用 + 同步到后端 settings.ui_language」。
    %(host)s.appendChild(U.el('div', { class: 'card' }, [
      U.el('h2', {}, [
        U.el('span', { html: Icons.svg('globe', { size: 17 }) }),
        U.el('span', { text: T('界面语言') }),
      ]),
      U.el('div', { class: 'card-hint',
        text: T('选择本应用界面的语言。首次打开时会跟随浏览器语言。') }),
      UI.langSelect(),
    ]));

    // 可访问目录
    const rootsCard = U.el('div', { class: 'card' });
    rootsCard.appendChild(U.el('h2', {}, [
      U.el('span', { html: Icons.svg('shield', { size: 17 }) }),
      U.el('span', { text: T('可访问目录（白名单）') }),
    ]));
    rootsCard.appendChild(U.el('div', { class: 'card-hint',
      text: T('本应用默认只读，且白名单初始为空。只有你在这里添加的目录，应用才能读取。') }));

    const rootList = U.el('div', { class: 'chips mb1' });
    function renderRoots() {
      rootList.innerHTML = '';
      if (!State.allowedRoots.length) {
        rootList.appendChild(U.el('span', { class: 'small faint', text: T('（当前为空，应用读不到任何目录）') }));
        return;
      }
      State.allowedRoots.forEach((root) => {
        const remove = U.el('button', { title: T('移除'), text: '×' });
        remove.addEventListener('click', async () => {
          const confirmed = await UI.confirm({
            title: T('移除可访问目录'),
            body: T('移除后应用将无法再读取：\n') + root + T('\n\n（不会删除任何文件）'),
            confirmText: T('移除'), danger: true,
          });
          if (!confirmed) return;
          State.allowedRoots = State.allowedRoots.filter((item) => item !== root);
          await saveRoots();
          renderRoots();
        });
        rootList.appendChild(U.el('span', { class: 'chip' }, [
          U.el('span', { text: root, title: root }), remove,
        ]));
      });
    }
    renderRoots();

    const addBtn = U.el('button', { class: 'btn primary' }, [
      U.el('span', { html: Icons.svg('plus', { size: 15 }) }),
      U.el('span', { text: T('添加目录') }),
    ]);
    addBtn.addEventListener('click', () => {
      UI.pickDir({
        title: T('选择允许本应用访问的目录'),
        start: State.allowedRoots[0] || '',
        onPick: async (path) => {
          if (State.allowedRoots.includes(path)) { UI.warn(T('已在列表中')); return; }
          State.allowedRoots.push(path);
          await saveRoots();
          renderRoots();
        },
      });
    });
    rootsCard.appendChild(U.el('div', { class: 'btn-row' }, [addBtn]));
    rootsCard.appendChild(U.el('div', { class: 'small muted mt1', text:
      T('路径校验方式：先 realpath 规范化，再比对白名单根。目录穿越（../）与指向白名单') +
      T('之外的软链接都会被拒绝。') }));
    host.appendChild(rootsCard);

    async function saveRoots() {
      try {
        await API.post('api/settings', { allowed_roots: State.allowedRoots });
      } catch (error) { UI.err(error, T('保存失败')); }
    }

    // 引擎
    const engineCard = U.el('div', { class: 'card' });
    engineCard.appendChild(U.el('h2', {}, [
      U.el('span', { html: Icons.svg('cpu', { size: 17 }) }),
      U.el('span', { text: T('处理引擎') }),
    ]));
    const detail = (State.engines && State.engines.engine_detail) || {};
    const rows = [
      [T('无损抽取（MP4 / MOV / M4A / MKV / WebM）'), T('内置实现，始终可用'), 'ok'],
      [T('纯音频元数据（WAV / FLAC / MP3 / OGG）'), T('内置实现，始终可用'), 'ok'],
      [T('转码预设'), detail.available ? (detail.version || T('可用')) : T('需要系统安装 ffmpeg'), detail.available ? 'ok' : 'neutral'],
    ];
    const table = U.el('table', { class: 'data' }, [
      U.el('thead', {}, [U.el('tr', {}, [
        U.el('th', { text: T('能力') }), U.el('th', { text: T('状态') }), U.el('th', { text: '' }),
      ])]),
    ]);
    const tbody = U.el('tbody', {});
    rows.forEach(([name, status, kind]) => {
      tbody.appendChild(U.el('tr', {}, [
        U.el('td', { text: name }),
        U.el('td', { class: 'small muted', text: status }),
        U.el('td', {}, [UI.badge(kind === 'ok' ? T('可用') : T('不可用'), kind)]),
      ]));
    });
    table.appendChild(tbody);
    engineCard.appendChild(table);
    host.appendChild(engineCard);

    // 维护
    const maintCard = U.el('div', { class: 'card' });
    maintCard.appendChild(U.el('h2', {}, [
      U.el('span', { html: Icons.svg('settings', { size: 17 }) }),
      U.el('span', { text: T('维护') }),
    ]));
    const purgeBtn = U.el('button', { class: 'btn' }, [
      U.el('span', { html: Icons.svg('archive', { size: 15 }) }),
      U.el('span', { text: T('清理已结束的任务记录') }),
    ]);
    purgeBtn.addEventListener('click', async () => {
      const confirmed = await UI.confirm({
        title: T('清理任务记录'),
        body: T('将删除已完成 / 失败 / 已取消的任务记录，只保留最近 200 条。\n') +
              T('媒体库缓存与已提取的文件不受影响。'),
        confirmText: T('清理'),
      });
      if (!confirmed) return;
      try {
        // 逐个删除过早的记录：后端提供 delete，这里只清最老的一批
        const data = await API.get('api/jobs?limit=500');
        const finished = (data.jobs || []).filter((job) => !job.is_active);
        const extra = finished.slice(200);
        for (const job of extra) {
          try { await API.del('api/jobs/' + job.id); } catch (error) { /* 忽略单条失败 */ }
        }
        UI.ok(T('已清理 ') + extra.length + T(' 条记录'));
        Jobs.tick();
      } catch (error) { UI.err(error); }
    });

    const aboutBtn = U.el('button', { class: 'btn' }, [
      U.el('span', { html: Icons.svg('info', { size: 15 }) }),
      U.el('span', { text: T('关于与隐私') }),
    ]);
    aboutBtn.addEventListener('click', async () => {
      let info = {};
      try { info = await API.get('api/app'); } catch (error) { /* 用默认值 */ }
      UI.modal({
        title: T('关于'), icon: 'info', wide: true,
        bodyHtml: `
          <p><b>${T('媒体音频提取器')}</b> v${U.esc(info.version || '')}</p>
          <p class="small muted">${T('把视频里的音轨无损抽出来。全部处理在本机离线完成。')}</p>
          <h3 class="mt2">${T('隐私')}</h3>
          <ul class="small">
            <li>${T('不联网、不上传任何文件或元数据')}</li>
            <li>${T('不收集使用统计、遥测或设备标识')}</li>
            <li>${T('源文件全程只读，不会被修改')}</li>
          </ul>
          <h3 class="mt2">${T('运行时写入位置')}</h3>
          <pre class="logview small">${U.esc(info.paths ? JSON.stringify(info.paths, null, 2) : '')}</pre>
          <p class="small muted mt1">${T('完整清单见包内 README.md 的「运行时写入路径清单（指引 12.9.6）」， 隐私政策见 PRIVACY.md。')}</p>`,
        buttons: [{ text: T('关闭') }],
      });
    });
    maintCard.appendChild(U.el('div', { class: 'btn-row' }, [purgeBtn, aboutBtn]));
    host.appendChild(maintCard);
  }

  /* ------------------------------------------------------------ 工具 */

  function baseName(path) {
    if (!path) return '';
    const parts = String(path).split(/[\\/]/);
    return parts[parts.length - 1] || path;
  }

  function dirName(path) {
    if (!path) return '';
    const index = String(path).replace(/[\\/]+$/, '').lastIndexOf('/');
    return index > 0 ? path.slice(0, index) : '';
  }

  function fmtDuration(seconds) {
    const total = Math.round(Number(seconds) || 0);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const pad = (n) => String(n).padStart(2, '0');
    return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
  }

  /* ------------------------------------------------------------ 启动 */

  async function boot() {
    Jobs.mountTaskbar(U.byId('taskbar'));
    Jobs.start(2500);

    const shell = Shell.init({
      overview: { label: T('概览'), icon: 'home', render: renderOverview },
      extract: { label: T('提取音轨'), icon: 'scissors', render: renderExtract },
      library: { label: T('媒体库'), icon: 'list', render: renderLibrary },
      jobs: { label: T('任务'), icon: 'activity', render: renderJobs },
      settings: { label: T('设置'), icon: 'settings', render: renderSettings },
    }, { defaultView: 'overview' });
    // 切语言后重渲染当前视图 —— 框架只换静态文案，动态渲染的部分要靠这个事件
    Shell.bindLanguage(shell);
    window.Shell = shell;

    try {
      const [engines, settings, app] = await Promise.all([
        API.get('api/engines'),
        API.get('api/settings'),
        Shell.loadAppInfo(),
      ]);
      State.engines = engines;
      State.presets = engines.presets || [];
      State.allowedRoots = (settings.settings && settings.settings.allowed_roots) || [];
      const detail = engines.engine_detail || {};
      U.byId('engine-meta').textContent = detail.available
        ? T('无损抽取 + 转码可用') : T('无损抽取可用（离线）');
      if (app) {
        const nameNode = U.byId('app-version');
        if (nameNode) nameNode.textContent = 'v' + app.version;
      }
    } catch (error) {
      U.byId('engine-meta').textContent = T('服务未就绪');
    }

    // 重新渲染当前视图，让加载到状态后界面完整
    shell.show(shell.current() || 'overview');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
