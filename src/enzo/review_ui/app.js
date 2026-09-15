const revisionId = decodeURIComponent(location.pathname.split('/').filter(Boolean).at(-1) || '');
const stages = ['INTENT', 'SPEC', 'PLAN', 'IMPLEMENTATION', 'VERIFICATION', 'DONE'];

const state = {
  detail: null,
  previous: null,
  drafts: [],
  selection: null,
  busy: false,
  view: 'rendered',
  webMcpRegistered: false,
  refreshTimer: null,
};

const el = Object.fromEntries(
  [
    'feature-key', 'feature-title', 'reviewer-id', 'back-to-plane', 'stage-track',
    'artifact-title', 'status-chip', 'content-hash', 'review-objective',
    'selection-guidance', 'view-rendered', 'view-source', 'view-diff',
    'artifact-rendered', 'artifact-source', 'artifact-diff', 'diff-summary',
    'execution-plan-panel',
    'execution-plan-source', 'revision-history', 'selection-action', 'composer',
    'selected-quote', 'feedback-section', 'feedback-comment', 'cancel-comment',
    'add-comment', 'feedback-list', 'empty-feedback', 'empty-feedback-title',
    'empty-feedback-copy', 'feedback-count',
    'resolved-feedback-section', 'resolved-feedback-list', 'resolved-feedback-count',
    'decision-help', 'manual-edit', 'address-agent', 'request-changes', 'approve',
    'manual-dialog', 'manual-content', 'save-manual', 'toast',
  ].map((id) => [id, document.getElementById(id)])
);

function requestId() {
  return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  let payload = null;
  try { payload = await response.json(); } catch { /* use status text */ }
  if (!response.ok) throw new Error(payload?.detail || response.statusText || 'Request failed');
  return payload;
}

function showToast(message, isError = false) {
  el.toast.textContent = message;
  el.toast.classList.toggle('error', isError);
  el.toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { el.toast.hidden = true; }, 4200);
}

function currentSection(content, offset) {
  const headings = [...content].slice(0, offset).join('').match(/^#{1,6}\s+.+$/gm);
  return headings?.at(-1)?.replace(/^#{1,6}\s+/, '').trim() || '';
}

function textOffset(root, node, offset) {
  const range = document.createRange();
  range.selectNodeContents(root);
  range.setEnd(node, offset);
  return [...range.toString()].length;
}

function sourceSlice(content, start, end) {
  return [...content].slice(start, end).join('');
}

function sourceLines(content) {
  const characters = [...content];
  const lines = [];
  let start = 0;
  for (let index = 0; index <= characters.length; index += 1) {
    if (index === characters.length || characters[index] === '\n') {
      lines.push({ text: characters.slice(start, index).join(''), start, end: index });
      start = index + 1;
    }
  }
  return lines;
}

function appendInlineMarkdown(parent, text) {
  const pattern = /(\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\((https?:\/\/[^)\s]+)\))/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    parent.append(document.createTextNode(text.slice(cursor, match.index)));
    if (match[2]) {
      const strong = document.createElement('strong');
      strong.textContent = match[2];
      parent.append(strong);
    } else if (match[3]) {
      const code = document.createElement('code');
      code.textContent = match[3];
      parent.append(code);
    } else {
      const link = document.createElement('a');
      link.textContent = match[4];
      link.href = match[5];
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      parent.append(link);
    }
    cursor = match.index + match[0].length;
  }
  parent.append(document.createTextNode(text.slice(cursor)));
}

function markSourceRange(node, start, end) {
  node.classList.add('md-block');
  node.dataset.sourceStart = String(start);
  node.dataset.sourceEnd = String(end);
  return node;
}

function startsMarkdownBlock(text) {
  return /^(#{1,6}\s+|```|\s*[-*+]\s+|\s*\d+[.)]\s+|>\s?)/.test(text);
}

function renderMarkdown(content) {
  const lines = sourceLines(content);
  const output = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.text.trim()) {
      index += 1;
      continue;
    }
    if (line.text.startsWith('```')) {
      const start = line.start;
      const codeLines = [];
      index += 1;
      while (index < lines.length && !lines[index].text.startsWith('```')) {
        codeLines.push(lines[index].text);
        index += 1;
      }
      const end = index < lines.length ? lines[index].end : lines.at(-1).end;
      if (index < lines.length) index += 1;
      const pre = markSourceRange(document.createElement('pre'), start, end);
      const code = document.createElement('code');
      code.textContent = codeLines.join('\n');
      pre.append(code);
      output.push(pre);
      continue;
    }
    const heading = line.text.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const node = markSourceRange(
        document.createElement(`h${heading[1].length}`), line.start, line.end
      );
      appendInlineMarkdown(node, heading[2]);
      output.push(node);
      index += 1;
      continue;
    }
    if (/^\s*[-*+]\s+/.test(line.text)) {
      const list = document.createElement('ul');
      while (index < lines.length && /^\s*[-*+]\s+/.test(lines[index].text)) {
        const itemLine = lines[index];
        const item = markSourceRange(
          document.createElement('li'), itemLine.start, itemLine.end
        );
        appendInlineMarkdown(item, itemLine.text.replace(/^\s*[-*+]\s+/, ''));
        list.append(item);
        index += 1;
      }
      output.push(list);
      continue;
    }
    if (/^\s*\d+[.)]\s+/.test(line.text)) {
      const list = document.createElement('ol');
      while (index < lines.length && /^\s*\d+[.)]\s+/.test(lines[index].text)) {
        const itemLine = lines[index];
        const item = markSourceRange(
          document.createElement('li'), itemLine.start, itemLine.end
        );
        appendInlineMarkdown(item, itemLine.text.replace(/^\s*\d+[.)]\s+/, ''));
        list.append(item);
        index += 1;
      }
      output.push(list);
      continue;
    }
    if (/^>\s?/.test(line.text)) {
      const quote = markSourceRange(document.createElement('blockquote'), line.start, line.end);
      appendInlineMarkdown(quote, line.text.replace(/^>\s?/, ''));
      output.push(quote);
      index += 1;
      continue;
    }

    const paragraphLines = [];
    const start = line.start;
    let end = line.end;
    while (
      index < lines.length
      && lines[index].text.trim()
      && !startsMarkdownBlock(lines[index].text)
    ) {
      paragraphLines.push(lines[index].text);
      end = lines[index].end;
      index += 1;
    }
    const paragraph = markSourceRange(document.createElement('p'), start, end);
    appendInlineMarkdown(paragraph, paragraphLines.join(' '));
    output.push(paragraph);
  }
  el['artifact-rendered'].replaceChildren(...output);
}

function occurrenceIndex(text, exact, ordinal) {
  let cursor = 0;
  for (let count = 0; count <= ordinal; count += 1) {
    const found = text.indexOf(exact, cursor);
    if (found < 0) return -1;
    if (count === ordinal) return found;
    cursor = found + exact.length;
  }
  return -1;
}

function renderedSelection(range, selection) {
  const startElement = range.startContainer.nodeType === Node.ELEMENT_NODE
    ? range.startContainer
    : range.startContainer.parentElement;
  const endElement = range.endContainer.nodeType === Node.ELEMENT_NODE
    ? range.endContainer
    : range.endContainer.parentElement;
  const startBlock = startElement?.closest('.md-block');
  const endBlock = endElement?.closest('.md-block');
  if (!startBlock || startBlock !== endBlock) return null;

  const exact = selection.toString();
  const visibleOffset = textOffset(startBlock, range.startContainer, range.startOffset);
  const visiblePrefix = [...startBlock.textContent].slice(0, visibleOffset).join('');
  const ordinal = visiblePrefix.split(exact).length - 1;
  const blockStart = Number(startBlock.dataset.sourceStart);
  const blockSource = sourceSlice(
    state.detail.content, blockStart, Number(startBlock.dataset.sourceEnd)
  );
  const utf16Index = occurrenceIndex(blockSource, exact, ordinal);
  if (utf16Index < 0) return null;
  const start = blockStart + [...blockSource.slice(0, utf16Index)].length;
  return selectionAt(exact, start);
}

function sourceSelection(range, selection) {
  const source = el['artifact-source'];
  if (!source.contains(range.commonAncestorContainer)) return null;
  return selectionAt(
    selection.toString(),
    textOffset(source, range.startContainer, range.startOffset)
  );
}

function selectionAt(exact, start) {
  const end = start + [...exact].length;
  if (
    !exact.trim()
    || [...exact].length > 10000
    || sourceSlice(state.detail.content, start, end) !== exact
  ) return null;
  return {
    exact,
    start_offset: start,
    end_offset: end,
    prefix: sourceSlice(state.detail.content, Math.max(0, start - 40), start),
    suffix: sourceSlice(state.detail.content, end, end + 40),
  };
}

function captureSelection() {
  if (!state.detail || !state.detail.review_write_enabled || state.detail.status !== 'REVIEW' || !state.detail.content) return;
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
    el['selection-action'].hidden = true;
    return;
  }
  const range = selection.getRangeAt(0);
  const captured = state.view === 'rendered'
    ? renderedSelection(range, selection)
    : state.view === 'source' ? sourceSelection(range, selection) : null;
  if (!captured) {
    el['selection-action'].hidden = true;
    return;
  }
  state.selection = captured;
  const rect = range.getBoundingClientRect();
  el['selection-action'].style.left = `${Math.min(innerWidth - 70, Math.max(70, rect.left + rect.width / 2))}px`;
  el['selection-action'].style.top = `${Math.max(46, rect.top)}px`;
  el['selection-action'].hidden = false;
}

function openComposer() {
  if (!state.selection) return;
  el['selected-quote'].textContent = state.selection.exact;
  el['feedback-section'].value = currentSection(state.detail.content, state.selection.start_offset);
  el['feedback-comment'].value = '';
  el.composer.hidden = false;
  el['selection-action'].hidden = true;
  el['feedback-comment'].focus();
}

function closeComposer() {
  el.composer.hidden = true;
  state.selection = null;
  window.getSelection()?.removeAllRanges();
}

function addDraft() {
  const comment = el['feedback-comment'].value.trim();
  if (!comment) {
    el['feedback-comment'].focus();
    showToast('Write a feedback comment first.', true);
    return;
  }
  state.drafts.push({
    id: requestId(),
    section: el['feedback-section'].value.trim() || null,
    comment,
    selection: state.selection,
  });
  closeComposer();
  renderFeedback();
  renderActions();
}

function setView(view) {
  if (view === 'diff' && el['view-diff'].disabled) return;
  state.view = view;
  for (const name of ['rendered', 'source', 'diff']) {
    const active = name === view;
    el[`view-${name}`].classList.toggle('active', active);
    el[`view-${name}`].setAttribute('aria-selected', String(active));
    el[`artifact-${name}`].hidden = !active;
  }
  el['selection-action'].hidden = true;
  window.getSelection()?.removeAllRanges();
  const status = state.detail?.status;
  if (!state.detail?.review_write_enabled) {
    el['selection-guidance'].textContent = 'This deployment is read-only.';
  } else if (status === 'GENERATING' || status === 'UPDATING') {
    el['selection-guidance'].textContent = 'Enzo is producing this revision. This page updates automatically.';
  } else if (status !== 'REVIEW') {
    el['selection-guidance'].textContent = 'This revision is not accepting new feedback.';
  } else if (view === 'diff') {
    el['selection-guidance'].textContent = 'Diff is read-only. Switch to Rendered or Source to attach feedback.';
  } else {
    el['selection-guidance'].textContent = 'Select text to attach precise feedback.';
  }
}

function renderDiff() {
  const currentLines = (state.detail.content || '').split('\n');
  const previousLines = (state.previous?.content || '').split('\n');
  if (!state.previous?.content || !state.detail.content) {
    el['artifact-diff'].replaceChildren();
    el['diff-summary'].textContent = state.previous
      ? 'Diff available when this revision is ready'
      : 'No previous revision';
    el['view-diff'].disabled = true;
    return;
  }

  let prefix = 0;
  while (
    prefix < previousLines.length
    && prefix < currentLines.length
    && previousLines[prefix] === currentLines[prefix]
  ) prefix += 1;
  let suffix = 0;
  while (
    suffix < previousLines.length - prefix
    && suffix < currentLines.length - prefix
    && previousLines[previousLines.length - 1 - suffix]
      === currentLines[currentLines.length - 1 - suffix]
  ) suffix += 1;

  const removed = previousLines.slice(prefix, previousLines.length - suffix);
  const added = currentLines.slice(prefix, currentLines.length - suffix);
  const rows = [];
  const appendRow = (kind, marker, text) => {
    const row = document.createElement('div');
    row.className = `diff-line ${kind}`;
    const sign = document.createElement('span');
    sign.className = 'diff-marker';
    sign.textContent = marker;
    const code = document.createElement('code');
    code.textContent = text || ' ';
    row.append(sign, code);
    rows.push(row);
  };
  previousLines.slice(0, prefix).forEach((line) => appendRow('unchanged', ' ', line));
  removed.forEach((line) => appendRow('removed', '−', line));
  added.forEach((line) => appendRow('added', '+', line));
  if (suffix) {
    currentLines.slice(currentLines.length - suffix).forEach(
      (line) => appendRow('unchanged', ' ', line)
    );
  }
  el['artifact-diff'].replaceChildren(...rows);
  el['view-diff'].disabled = false;
  el['diff-summary'].textContent = `Compared with r${state.previous.revision_no} · +${added.length} −${removed.length}`;
}

function highlightLocation(location) {
  if (!location) return;
  setView('source');
  const source = el['artifact-source'];
  const node = source.firstChild;
  if (!node || node.nodeType !== Node.TEXT_NODE) return;
  const range = document.createRange();
  const codepoints = [...node.data];
  const start = codepoints.slice(0, location.start_offset).join('').length;
  const end = codepoints.slice(0, location.end_offset).join('').length;
  range.setStart(node, start);
  range.setEnd(node, end);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  source.focus({ preventScroll: true });
  const rect = range.getBoundingClientRect();
  window.scrollTo({ top: window.scrollY + rect.top - 130, behavior: 'smooth' });
}

function feedbackCard(item, isDraft = false, addressedHere = false) {
  const card = document.createElement('article');
  card.className = `feedback-card${isDraft ? ' draft' : ''}${addressedHere ? ' resolved' : ''}${item.location || item.selection ? ' has-location' : ''}`;
  const top = document.createElement('div');
  top.className = 'feedback-topline';
  const section = document.createElement('span');
  section.className = 'feedback-section';
  section.textContent = item.section || 'General';
  const status = document.createElement(isDraft ? 'button' : 'span');
  status.className = isDraft ? 'remove-draft' : 'feedback-state';
  status.textContent = isDraft
    ? 'Remove'
    : addressedHere ? `${item.resolution_type || 'RESOLVED'} · RESOLVED` : item.status;
  if (isDraft) {
    status.type = 'button';
    status.addEventListener('click', (event) => {
      event.stopPropagation();
      state.drafts = state.drafts.filter((draft) => draft.id !== item.id);
      renderFeedback();
      renderActions();
    });
  }
  top.append(section, status);
  const comment = document.createElement('p');
  comment.className = 'feedback-comment';
  comment.textContent = item.comment;
  card.append(top, comment);
  const location = item.location || item.selection;
  if (location) {
    const quote = document.createElement('blockquote');
    quote.className = 'feedback-quote';
    quote.textContent = location.exact;
    card.append(quote);
    card.addEventListener('click', () => {
      if (item.artifact_revision_id && item.artifact_revision_id !== revisionId) {
        window.location.assign(`/artifacts/${item.artifact_revision_id}`);
      } else {
        highlightLocation(location);
      }
    });
    if (addressedHere) card.title = 'Open the revision where this feedback was created';
  }
  return card;
}

function renderFeedback() {
  const persisted = state.detail?.feedback || [];
  const items = [...persisted, ...state.drafts];
  const resolved = state.detail?.resolved_feedback || [];
  el['feedback-list'].replaceChildren(...items.map((item, index) => feedbackCard(item, index >= persisted.length)));
  el['empty-feedback'].hidden = items.length > 0;
  el['empty-feedback'].classList.toggle('compact', resolved.length > 0);
  el['empty-feedback-title'].textContent = resolved.length ? 'No new feedback' : 'No feedback yet';
  el['empty-feedback-copy'].textContent = resolved.length
    ? 'Previously requested changes are shown below.'
    : 'Select text to start a review.';
  el['feedback-list'].hidden = items.length === 0;
  const activeCount = persisted.filter((item) => item.status === 'OPEN').length + state.drafts.length;
  el['feedback-count'].textContent = String(activeCount);

  el['resolved-feedback-section'].hidden = resolved.length === 0;
  el['resolved-feedback-count'].textContent = String(resolved.length);
  el['resolved-feedback-list'].replaceChildren(
    ...resolved.map((item) => feedbackCard(item, false, true))
  );
}

function renderStages() {
  const current = state.detail?.feature_stage;
  const index = stages.indexOf(current);
  el['stage-track'].replaceChildren(...stages.map((stage, position) => {
    const item = document.createElement('div');
    item.className = `stage${position < index ? ' complete' : ''}${position === index ? ' current' : ''}`;
    item.innerHTML = '<span class="stage-dot" aria-hidden="true"></span>';
    const label = document.createElement('span');
    label.textContent = stage;
    item.append(label);
    return item;
  }));
}

function renderHistory() {
  el['revision-history'].replaceChildren(...state.detail.history.map((revision) => {
    const link = document.createElement('a');
    link.className = `revision-link${revision.id === revisionId ? ' current' : ''}`;
    link.href = `/artifacts/${revision.id}`;
    link.textContent = `r${revision.revision_no}`;
    link.title = revision.status;
    return link;
  }));
}

function setBusy(busy) {
  state.busy = busy;
  for (const id of ['manual-edit', 'address-agent', 'request-changes', 'approve', 'save-manual']) {
    el[id].disabled = busy;
  }
}

function renderActions() {
  const status = state.detail?.status;
  if (state.detail && !state.detail.review_write_enabled) {
    for (const id of ['approve', 'request-changes', 'manual-edit', 'address-agent']) {
      el[id].hidden = true;
    }
    el['decision-help'].textContent = 'Read-only deployment. Review mutations must be explicitly enabled on a trusted host.';
    return;
  }
  const review = status === 'REVIEW';
  const changes = status === 'CHANGES_REQUESTED';
  el.approve.hidden = !review;
  el['request-changes'].hidden = !review;
  el['request-changes'].disabled = state.busy || state.drafts.length === 0;
  el.approve.disabled = state.busy || state.drafts.length > 0;
  el['manual-edit'].hidden = !changes;
  el['address-agent'].hidden = !changes;
  if (review) {
    el['decision-help'].textContent = state.drafts.length
      ? `${state.drafts.length} draft feedback item(s). Submit them together or remove them to approve.`
      : 'Approval is an explicit human decision and advances the feature stage.';
  } else if (changes) {
    el['decision-help'].textContent = 'Choose who addresses the open feedback. Both paths create a new immutable revision.';
  } else {
    el['decision-help'].textContent = status === 'APPROVED'
      ? 'This historical revision is approved and read-only.'
      : 'This revision is not currently awaiting a human decision.';
  }
}

function reviewObjective(detail) {
  const objectives = {
    INTENT: 'Confirm the problem, desired outcome, scope, and constraints.',
    SPEC: 'Confirm the requirements, design decisions, edge cases, and verification contract.',
    PLAN: 'Confirm the execution sequence, file-level changes, dependencies, and evidence mapping.',
  };
  if (detail.status === 'CHANGES_REQUESTED') {
    return 'Feedback is recorded. Choose whether a human or agent creates the next revision.';
  }
  if (detail.status === 'GENERATING' || detail.status === 'UPDATING') {
    return 'Enzo is producing this revision. No human decision is required yet.';
  }
  if (detail.status === 'APPROVED') {
    return 'This immutable revision was approved and is available for historical inspection.';
  }
  return objectives[detail.artifact_type] || 'Confirm this artifact before the workflow proceeds.';
}

function render() {
  const detail = state.detail;
  document.title = `${detail.display_name} r${detail.revision_no} · Enzo`;
  el['feature-key'].textContent = detail.external_id;
  el['feature-title'].textContent = detail.title;
  el['reviewer-id'].textContent = detail.review_actor_id;
  el['artifact-title'].textContent = `${detail.display_name} · revision ${detail.revision_no}`;
  el['status-chip'].textContent = detail.status;
  el['status-chip'].className = `status-chip ${detail.status.toLowerCase().replaceAll('_', '-')}`;
  el['content-hash'].textContent = detail.content_sha256 ? `sha256 ${detail.content_sha256.slice(0, 12)}` : 'content pending';
  el['review-objective'].textContent = reviewObjective(detail);
  const content = detail.content || 'Artifact content is not ready yet. The worker may still be generating it.';
  el['artifact-source'].textContent = content;
  renderMarkdown(content);
  renderDiff();
  if (state.view === 'diff' && !state.previous) state.view = 'rendered';
  setView(state.view);
  el['execution-plan-panel'].hidden = !detail.execution_plan_content;
  el['execution-plan-source'].textContent = detail.execution_plan_content || '';
  renderStages();
  renderHistory();
  renderFeedback();
  renderActions();
}

async function submitAction(action, feedback = []) {
  setBusy(true);
  renderActions();
  try {
    const payload = await api(`/api/artifact-revisions/${revisionId}/review-actions`, {
      method: 'POST',
      body: JSON.stringify({ action, feedback, request_id: requestId() }),
    });
    showToast(payload.event.message);
    const nextRevision = payload.feature?.artifact?.current_revision_id;
    if (nextRevision && nextRevision !== revisionId) {
      location.assign(`/artifacts/${nextRevision}`);
      return payload;
    }
    state.drafts = [];
    await load();
    return payload;
  } catch (error) {
    showToast(error.message, true);
    await load();
  } finally {
    setBusy(false);
    renderActions();
  }
}

function registerWebMcpTools() {
  const context = document.modelContext;
  if (state.webMcpRegistered || !context?.registerTool) return;
  state.webMcpRegistered = true;
  const lifecycle = new AbortController();
  window.addEventListener('pagehide', () => lifecycle.abort(), { once: true });
  const report = (error) => console.warn('Unable to register Enzo WebMCP tool', error);

  const registrations = [
    context.registerTool({
      name: 'read_enzo_artifact_revision',
      title: 'Read Enzo artifact revision',
      description: 'Read the immutable Markdown revision, review status, and structured feedback currently visible in Enzo.',
      inputSchema: { type: 'object', properties: {}, additionalProperties: false },
      annotations: { readOnlyHint: true, untrustedContentHint: true },
      execute: async () => ({
        id: state.detail.id,
        artifact: state.detail.display_name,
        revision: state.detail.revision_no,
        status: state.detail.status,
        content: state.detail.content,
        feedback: state.detail.feedback,
        resolved_feedback: state.detail.resolved_feedback,
      }),
    }, { signal: lifecycle.signal }),
    context.registerTool({
      name: 'submit_enzo_artifact_review',
      title: 'Submit Enzo artifact review',
      description: 'Approve the visible revision, request changes with a batch of explicit feedback items, or ask an agent to address already-requested changes.',
      inputSchema: {
        type: 'object',
        properties: {
          action: { type: 'string', enum: ['APPROVE', 'REQUEST_CHANGES', 'ADDRESS_WITH_AGENT'] },
          feedback: {
            type: 'array',
            maxItems: 100,
            items: {
              type: 'object',
              properties: {
                section: { type: ['string', 'null'], maxLength: 200 },
                comment: { type: 'string', minLength: 1, maxLength: 10000 },
                selection: {
                  type: ['object', 'null'],
                  properties: {
                    exact: { type: 'string', minLength: 1, maxLength: 10000 },
                    start_offset: { type: 'integer', minimum: 0 },
                    end_offset: { type: 'integer', minimum: 1 },
                    prefix: { type: 'string', maxLength: 80 },
                    suffix: { type: 'string', maxLength: 80 },
                  },
                  required: ['exact', 'start_offset', 'end_offset'],
                  additionalProperties: false,
                },
              },
              required: ['comment'],
              additionalProperties: false,
            },
          },
        },
        required: ['action'],
        additionalProperties: false,
      },
      annotations: { readOnlyHint: false, untrustedContentHint: true },
      execute: async (input) => {
        const allowed = ['APPROVE', 'REQUEST_CHANGES', 'ADDRESS_WITH_AGENT'];
        if (!input || !allowed.includes(input.action)) throw new Error('Invalid review action');
        const payload = await submitAction(input.action, input.feedback || []);
        return {
          outcome: payload?.event?.outcome,
          message: payload?.event?.message,
          current_revision_id: payload?.feature?.artifact?.current_revision_id,
        };
      },
    }, { signal: lifecycle.signal }),
  ];
  for (const registration of registrations) Promise.resolve(registration).catch(report);
}

async function saveManualRevision() {
  setBusy(true);
  try {
    const payload = await api(`/api/artifact-revisions/${revisionId}/manual-revisions`, {
      method: 'POST',
      body: JSON.stringify({
        content: el['manual-content'].value,
        execution_plan: state.detail.execution_plan,
        request_id: requestId(),
      }),
    });
    showToast(payload.event.message);
    const nextRevision = payload.feature?.artifact?.current_revision_id;
    if (nextRevision) location.assign(`/artifacts/${nextRevision}`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function load() {
  clearTimeout(state.refreshTimer);
  state.refreshTimer = null;
  state.detail = await api(`/api/artifact-revisions/${revisionId}`);
  const previousRevision = state.detail.history
    .filter((revision) => revision.revision_no < state.detail.revision_no)
    .at(-1);
  state.previous = previousRevision
    ? await api(`/api/artifact-revisions/${previousRevision.id}`)
    : null;
  render();
  registerWebMcpTools();
  if (state.detail.status === 'GENERATING' || state.detail.status === 'UPDATING') {
    state.refreshTimer = setTimeout(() => {
      load().catch((error) => showToast(error.message, true));
    }, 1800);
  }
}

document.addEventListener('selectionchange', captureSelection);
el['selection-action'].addEventListener('click', openComposer);
el['cancel-comment'].addEventListener('click', closeComposer);
el['add-comment'].addEventListener('click', addDraft);
el.approve.addEventListener('click', () => submitAction('APPROVE'));
el['request-changes'].addEventListener('click', () => submitAction(
  'REQUEST_CHANGES',
  state.drafts.map(({ section, comment, selection }) => ({ section, comment, selection }))
));
el['address-agent'].addEventListener('click', () => submitAction('ADDRESS_WITH_AGENT'));
el['manual-edit'].addEventListener('click', () => {
  el['manual-content'].value = state.detail.content || '';
  el['manual-dialog'].showModal();
});
el['save-manual'].addEventListener('click', saveManualRevision);
for (const view of ['rendered', 'source', 'diff']) {
  el[`view-${view}`].addEventListener('click', () => setView(view));
}
if (document.referrer) {
  try {
    const referrer = new URL(document.referrer);
    el['back-to-plane'].hidden = referrer.origin === location.origin;
  } catch { /* an invalid referrer stays hidden */ }
}
el['back-to-plane'].addEventListener('click', () => history.back());
window.addEventListener('pagehide', () => clearTimeout(state.refreshTimer), { once: true });

load().catch((error) => {
  showToast(error.message, true);
  el['artifact-title'].textContent = 'Unable to load artifact';
});
