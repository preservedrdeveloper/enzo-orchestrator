const revisionId = decodeURIComponent(location.pathname.split('/').filter(Boolean).at(-1) || '');
const stages = ['INTENT', 'SPEC', 'PLAN', 'IMPLEMENTATION', 'VERIFICATION', 'DONE'];

const state = {
  detail: null,
  drafts: [],
  selection: null,
  busy: false,
  webMcpRegistered: false,
};

const el = Object.fromEntries(
  [
    'feature-key', 'feature-title', 'reviewer-id', 'stage-track', 'artifact-title',
    'status-chip', 'content-hash', 'artifact-source', 'execution-plan-panel',
    'execution-plan-source', 'revision-history', 'selection-action', 'composer',
    'selected-quote', 'feedback-section', 'feedback-comment', 'cancel-comment',
    'add-comment', 'feedback-list', 'empty-feedback', 'feedback-count',
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

function captureSelection() {
  if (!state.detail || !state.detail.review_write_enabled || state.detail.status !== 'REVIEW' || !state.detail.content) return;
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
    el['selection-action'].hidden = true;
    return;
  }
  const range = selection.getRangeAt(0);
  const source = el['artifact-source'];
  if (!source.contains(range.commonAncestorContainer)) {
    el['selection-action'].hidden = true;
    return;
  }
  const start = textOffset(source, range.startContainer, range.startOffset);
  const end = textOffset(source, range.endContainer, range.endOffset);
  const exact = sourceSlice(state.detail.content, start, end);
  if (!exact.trim()) {
    el['selection-action'].hidden = true;
    return;
  }
  state.selection = {
    exact,
    start_offset: start,
    end_offset: end,
    prefix: sourceSlice(state.detail.content, Math.max(0, start - 40), start),
    suffix: sourceSlice(state.detail.content, end, end + 40),
  };
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

function highlightLocation(location) {
  if (!location) return;
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

function feedbackCard(item, isDraft = false) {
  const card = document.createElement('article');
  card.className = `feedback-card${isDraft ? ' draft' : ''}${item.location || item.selection ? ' has-location' : ''}`;
  const top = document.createElement('div');
  top.className = 'feedback-topline';
  const section = document.createElement('span');
  section.className = 'feedback-section';
  section.textContent = item.section || 'General';
  const status = document.createElement(isDraft ? 'button' : 'span');
  status.className = isDraft ? 'remove-draft' : 'feedback-state';
  status.textContent = isDraft ? 'Remove' : item.status;
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
    card.addEventListener('click', () => highlightLocation(location));
  }
  return card;
}

function renderFeedback() {
  const persisted = state.detail?.feedback || [];
  const items = [...persisted, ...state.drafts];
  el['feedback-list'].replaceChildren(...items.map((item, index) => feedbackCard(item, index >= persisted.length)));
  el['empty-feedback'].hidden = items.length > 0;
  el['feedback-list'].hidden = items.length === 0;
  el['feedback-count'].textContent = String(items.length);
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
  el['artifact-source'].textContent = detail.content || 'Artifact content is not ready yet. The worker may still be generating it.';
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
  state.detail = await api(`/api/artifact-revisions/${revisionId}`);
  render();
  registerWebMcpTools();
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

load().catch((error) => {
  showToast(error.message, true);
  el['artifact-title'].textContent = 'Unable to load artifact';
});
