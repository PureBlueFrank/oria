/* Real screenshot pixels, with separate editorial captions and source mapping. */
(() => {
  'use strict';
  const data = window.ORIA_SCREENSHOT_TOUR;
  const byId = id => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const capture = params.has('capture');
  if (capture) document.body.classList.add('capture');
  const assets = new Map(data.assets.map(asset => [asset.id, asset]));
  const count = data.scenes.length;
  const initial = Number(params.get('scene'));
  let current = Number.isInteger(initial) ? Math.min(count - 1, Math.max(0, initial)) : 0;
  let playing = false;
  let timer;
  const starts = data.scenes.map((_, i) => data.scenes.slice(0, i).reduce((sum, s) => sum + s.seconds, 0));
  const total = data.scenes.reduce((sum, s) => sum + s.seconds, 0);
  const clock = seconds => `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
  const node = (tag, value, className = '') => {
    const element = document.createElement(tag);
    if (value !== undefined) element.textContent = value;
    element.className = className;
    return element;
  };
  const steps = ['规则草案', '招商审批', '招商发布', '报名圈品', '逐级确认', '关联券', '异步选品', '投放审批', 'C端投放', '通知闭环'];
  function infoCard(card) {
    const section = node('section', undefined, 'info-card');
    section.append(node('h2', card[0]), node('p', card[1]));
    return section;
  }
  function screenshot(scene) {
    const asset = assets.get(scene.asset);
    const frame = node('div', undefined, 'image-frame');
    const button = node('button');
    button.type = 'button';
    button.setAttribute('aria-label', `放大截图：${scene.title}`);
    const image = node('img');
    image.src = `screenshots/${asset.id}.png`;
    image.alt = `${scene.title}。${scene.subtitle} 来源 ${asset.source}，${asset.part}。`;
    image.width = asset.crop[2]; image.height = asset.crop[3];
    button.append(image); frame.append(button);
    button.onclick = () => {
      pause();
      byId('zoom-image').src = image.src;
      byId('zoom-image').alt = image.alt;
      byId('zoom-caption').textContent = `真实飞书截图 · ${asset.source} · ${asset.part}。${data.boundary}`;
      byId('zoom').showModal();
    };
    return frame;
  }
  function render() {
    const scene = data.scenes[current];
    byId('chapter').textContent = scene.chapter;
    byId('counter').textContent = `${String(current + 1).padStart(2, '0')} / ${count}`;
    byId('title').textContent = scene.title;
    byId('subtitle').textContent = scene.subtitle;
    byId('boundary').textContent = data.boundary;
    byId('workflow').replaceChildren(...steps.map((label, index) => {
      const step = node('li', undefined, scene.stage === index + 1 ? 'current' : scene.stage > index + 1 ? 'done' : '');
      step.append(node('em', String(index + 1).padStart(2, '0')), node('span', label));
      if (scene.stage === index + 1) step.setAttribute('aria-current', 'step');
      return step;
    }));
    const content = byId('content'); content.className = `content ${scene.kind}`; content.replaceChildren();
    if (scene.kind === 'screenshot' || scene.kind === 'cover') content.append(screenshot(scene));
    if (scene.kind === 'cover') {
      const cards = node('div', undefined, 'cover-cards'); cards.append(...scene.cards.map(infoCard)); content.append(cards);
    }
    if (scene.kind === 'info') content.append(...scene.cards.map(infoCard));
    if (scene.kind === 'flow') content.append(...scene.cards.map((card, index) => {
      const element = node('div', undefined, `flow-card ${[1, 7].includes(index) ? 'gate' : ''}`);
      element.append(node('b', card[0]), node('span', card[1])); return element;
    }));
    const asset = assets.get(scene.asset);
    byId('source-type').textContent = asset ? '真实飞书截图 · 裁剪节选' : scene.kind === 'flow' ? '流程说明 · 对应已交付工作流' : '工程能力说明 · 文档依据';
    byId('source-detail').textContent = asset ? `来源 ${asset.source} ｜ ${asset.part}${scene.kind === 'cover' ? ' ｜ 概览文字依据项目文档' : ''}` : 'ARCHITECTURE.md / ROADMAP.md ｜ 不作为新增实测证据';
    byId('progress').style.width = `${(current + 1) / count * 100}%`;
    byId('scene-picker').value = String(current);
    byId('time').textContent = `${clock(starts[current])} / ${clock(total)}`;
    byId('previous').disabled = current === 0;
    byId('next').disabled = current === count - 1;
    document.documentElement.dataset.scene = String(current);
    document.title = `ORIA · ${scene.title}`;
  }
  function pause() { clearTimeout(timer); playing = false; byId('play').textContent = '播放'; }
  function schedule() {
    clearTimeout(timer);
    if (!playing) return;
    timer = setTimeout(() => {
      if (current === count - 1) { pause(); return; }
      current += 1; render(); schedule();
    }, data.scenes[current].seconds * 1000);
  }
  function move(target) { pause(); current = Math.max(0, Math.min(count - 1, target)); render(); }
  byId('previous').onclick = () => move(current - 1);
  byId('next').onclick = () => move(current + 1);
  byId('play').onclick = () => {
    if (playing) { pause(); return; }
    if (current === count - 1) { current = 0; render(); }
    playing = true; byId('play').textContent = '暂停'; schedule();
  };
  byId('scene-picker').replaceChildren(...data.scenes.map((scene, index) => {
    const option = node('option', `${String(index + 1).padStart(2, '0')} · ${scene.chapter} · ${scene.title}`);
    option.value = String(index); return option;
  }));
  byId('scene-picker').onchange = event => move(Number(event.target.value));
  byId('close').onclick = () => byId('zoom').close();
  document.addEventListener('keydown', event => {
    if (byId('zoom').open || ['SELECT', 'INPUT', 'BUTTON'].includes(event.target.tagName)) return;
    if (event.key === 'ArrowLeft') { event.preventDefault(); move(current - 1); }
    if (event.key === 'ArrowRight') { event.preventDefault(); move(current + 1); }
  });
  document.addEventListener('visibilitychange', () => { if (document.hidden) pause(); });
  function resize() {
    const scale = capture ? 1 : Math.min(1, byId('viewport').clientWidth / 1280);
    byId('stage').style.transform = `scale(${scale})`;
    byId('viewport').style.height = `${1040 * scale}px`;
  }
  window.addEventListener('resize', resize);
  render(); resize();
})();
