const executionId = decodeURIComponent(location.pathname.split('/').filter(Boolean).at(-1) || '');
const stages = ['INTENT', 'SPEC', 'PLAN', 'IMPLEMENTATION', 'VERIFICATION', 'DONE'];

const state = {
  detail: null,
  revision: null,
  drafts: [],
  busy: false,
  view: 'overview',
  refreshTimer: null,
  webMcpRegistered: false,
};

const el = Object.fromEntries(
  [
    'feature-key', 'feature-title', 'reviewer-id', 'back-to-plane', 'stage-track',
    'implementation-title', 'status-chip', 'head-sha', 'execution-status', 'branch',
    'base-sha', 'agent', 'review-objective', 'review-guidance', 'view-overview',
    'view-patch', 'view-evidence', 'implementation-overview', 'implementation-patch',
    'implementation-evidence', 'diff-summary', 'checks-summary', 'changed-files-count',
    'worktree-state', 'changed-files', 'open-patch', 'agent-summary', 'patch-content',
    'evidence-list', 'revision-history', 'recovery-panel', 'recovery-state',
    'recovery-title', 'recovery-description', 'recovery-error', 'inspect-failure',
    'retry-execution', 'abandon-execution', 'abandon-dialog', 'confirm-abandon',
    'new-feedback', 'composer', 'feedback-section',
    'feedback-comment', 'cancel-comment', 'add-comment', 'feedback-list',
    'empty-feedback', 'empty-feedback-title', 'empty-feedback-copy', 'feedback-count',
    'resolved-feedback-section', 'resolved-feedback-count', 'resolved-feedback-list',
    'decision-help', 'address-agent', 'request-changes', 'approve', 'toast',
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

function selectedRevisionId() {
  return new URLSearchParams(location.search).get('revision');
}

function chooseRevision(detail) {
  const requested = selectedRevisionId();
  return detail.implementation_revisions.find((revision) => revision.id === requested)
    || detail.implementation_revisions.find(
      (revision) => revision.id === detail.current_revision_id
    )
    || detail.implementation_revisions.at(-1)
    || null;
}

function shortSha(value) {
  return value ? value.slice(0, 12) : 'pending';
}

function parsePatch(content) {
  const files = [];
  let current = null;
  for (const line of (content || '').split('\n')) {
    if (line.startsWith('diff --git ')) {
      const match = line.match(/^diff --git a\/(.+) b\/(.+)$/);
      current = {
        path: match?.[2] || line.replace('diff --git ', ''),
        additions: 0,
        deletions: 0,
      };
      files.push(current);
    } else if (current && line.startsWith('+') && !line.startsWith('+++')) {
      current.additions += 1;
    } else if (current && line.startsWith('-') && !line.startsWith('---')) {
      current.deletions += 1;
    }
  }
  return files;
}

function setView(view) {
  state.view = view;
  for (const name of ['overview', 'patch', 'evidence']) {
    const active = name === view;
    el[`view-${name}`].classList.toggle('active', active);
    el[`view-${name}`].setAttribute('aria-selected', String(active));
    el[`implementation-${name}`].hidden = !active;
  }
}

function renderStages() {
  const current = state.detail.feature_stage;
  const currentIndex = stages.indexOf(current);
  el['stage-track'].replaceChildren(...stages.map((stage, index) => {
    const item = document.createElement('div');
    item.className = `stage${index < currentIndex ? ' complete' : ''}${index === currentIndex ? ' current' : ''}`;
    const dot = document.createElement('span');
    dot.className = 'stage-dot';
    dot.setAttribute('aria-hidden', 'true');
    const label = document.createElement('span');
    label.textContent = stage;
    item.append(dot, label);
    return item;
  }));
}

function renderPatch() {
  const content = state.revision?.diff || '';
  const files = parsePatch(content);
  const additions = files.reduce((total, file) => total + file.additions, 0);
  const deletions = files.reduce((total, file) => total + file.deletions, 0);
  const rows = content ? content.split('\n').map((line) => {
    const row = document.createElement('div');
    row.className = 'patch-line';
    if (line.startsWith('diff --git ')) row.classList.add('file');
    else if (line.startsWith('@@')) row.classList.add('hunk');
    else if (line.startsWith('+') && !line.startsWith('+++')) row.classList.add('added');
    else if (line.startsWith('-') && !line.startsWith('---')) row.classList.add('removed');
    else if (line.startsWith('index ') || line.startsWith('---') || line.startsWith('+++')) {
      row.classList.add('meta');
    }
    const code = document.createElement('code');
    code.textContent = line || ' ';
    row.append(code);
    return row;
  }) : [];
  if (!rows.length) {
    const empty = document.createElement('div');
    empty.className = 'view-empty';
    empty.textContent = state.revision?.status === 'UPDATING'
      ? 'The patch will appear when this revision is ready.'
      : 'No patch was recorded for this revision.';
    rows.push(empty);
  }
  el['patch-content'].replaceChildren(...rows);
  el['changed-files-count'].textContent = String(files.length);
  el['diff-summary'].textContent = files.length
    ? `${files.length} changed file${files.length === 1 ? '' : 's'} · +${additions} −${deletions}`
    : 'Diff pending';
  el['changed-files'].replaceChildren(...files.map((file) => {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'changed-file';
    const path = document.createElement('code');
    path.textContent = file.path;
    const stat = document.createElement('span');
    const additions = document.createElement('strong');
    additions.className = 'additions';
    additions.textContent = `+${file.additions}`;
    const deletions = document.createElement('strong');
    deletions.className = 'deletions';
    deletions.textContent = `−${file.deletions}`;
    stat.append(additions, deletions);
    row.append(path, stat);
    row.addEventListener('click', () => setView('patch'));
    return row;
  }));
  if (!files.length) {
    const empty = document.createElement('div');
    empty.className = 'view-empty compact';
    empty.textContent = 'Changed files will appear after deterministic verification.';
    el['changed-files'].append(empty);
  }
}

function currentEvidence() {
  return state.detail.evidence.filter((item) => (
    item.implementation_revision_id === null
    || item.implementation_revision_id === state.revision?.id
  ));
}

function revisionAgentRun() {
  return (state.detail.result.agent_runs || []).findLast(
    (item) => item.implementation_revision === state.revision?.revision_no
  );
}

function renderEvidence() {
  const items = currentEvidence();
  const checks = state.revision
    ? items.filter((item) => item.implementation_revision_id !== null)
    : items;
  const passed = checks.filter((item) => item.status === 'PASS').length;
  const failed = checks.filter((item) => item.status === 'FAIL').length;
  el['checks-summary'].textContent = checks.length
    ? `${passed}/${checks.length} passed${failed ? ` · ${failed} failed` : ''}`
    : 'Pending';
  el['checks-summary'].className = failed ? 'check-failed' : passed ? 'check-passed' : '';

  el['evidence-list'].replaceChildren(...items.map((item) => {
    const card = document.createElement('details');
    card.className = `evidence-card ${item.status.toLowerCase()}`;
    const summary = document.createElement('summary');
    const identity = document.createElement('span');
    const name = document.createElement('strong');
    name.textContent = item.name;
    const type = document.createElement('small');
    type.textContent = item.evidence_type;
    identity.append(name, type);
    const result = document.createElement('span');
    result.className = 'evidence-result';
    const duration = Number(item.duration_seconds || 0).toFixed(2);
    result.textContent = `${item.status} · ${duration}s`;
    summary.append(identity, result);
    const command = document.createElement('code');
    command.className = 'evidence-command';
    try {
      command.textContent = JSON.parse(item.command_json || '[]').join(' ');
    } catch {
      command.textContent = 'Command metadata unavailable';
    }
    const log = document.createElement('pre');
    log.textContent = item.log;
    card.append(summary, command, log);
    return card;
  }));
  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'view-empty';
    empty.textContent = state.revision?.status === 'UPDATING'
      ? 'Verification evidence will appear after the coding agent finishes.'
      : 'No verification evidence was recorded for this revision.';
    el['evidence-list'].append(empty);
  }
}

function renderAgentSummary() {
  const run = revisionAgentRun();
  const fields = [];
  if (run) {
    fields.push(['Runner', run.runner?.name || state.detail.agent]);
    fields.push(['Adapter', run.runner?.kind || 'unknown']);
    if (run.runner?.provider) fields.push(['Provider', run.runner.provider]);
    if (run.runner?.model) fields.push(['Model', run.runner.model]);
    fields.push(['Role', run.role]);
  } else {
    fields.push(['Runner', state.detail.agent || 'pending']);
    fields.push(['Status', state.revision?.status || state.detail.status]);
  }
  el['agent-summary'].replaceChildren(...fields.map(([label, value]) => {
    const item = document.createElement('div');
    const term = document.createElement('span');
    term.textContent = label;
    const description = document.createElement('strong');
    description.textContent = value;
    item.append(term, description);
    return item;
  }));
  if (state.detail.error && !state.detail.recovery?.operation) {
    const error = document.createElement('pre');
    error.className = 'execution-error';
    error.textContent = state.detail.error;
    el['agent-summary'].append(error);
  }
}

function renderRecovery() {
  const recovery = state.detail.recovery;
  const visible = state.detail.status === 'FAILED'
    || state.detail.status === 'CANCELLED'
    || Boolean(recovery?.operation);
  el['recovery-panel'].hidden = !visible;
  if (!visible) return;

  const operation = recovery?.operation?.replaceAll('_', ' ') || 'EXECUTION';
  const operationStatus = recovery?.status || state.detail.status;
  el['recovery-state'].textContent = `${operation} · ${operationStatus}`;
  if (state.detail.status === 'CANCELLED') {
    if (operationStatus === 'FAILED') {
      el['recovery-title'].textContent = 'Worktree cleanup needs attention';
      el['recovery-description'].textContent = 'The execution remains cancelled. Retry the deterministic cleanup after inspecting the failure.';
    } else {
      el['recovery-title'].textContent = operationStatus === 'SUCCEEDED'
        ? 'Execution abandoned'
        : 'Execution abandonment in progress';
      el['recovery-description'].textContent = state.detail.worktree_present
        ? 'The execution is cancelled. Deterministic worktree cleanup is pending.'
        : 'The execution is cancelled and its isolated worktree is no longer present.';
    }
  } else if (operationStatus === 'PENDING' || operationStatus === 'RUNNING') {
    el['recovery-title'].textContent = 'Recovery is running';
    el['recovery-description'].textContent = 'Enzo is retrying the failed deterministic operation in the same worktree.';
  } else {
    el['recovery-title'].textContent = 'Execution needs attention';
    el['recovery-description'].textContent = state.detail.worktree_present
      ? 'The failure is recorded and the isolated worktree is preserved for inspection.'
      : 'The failure is recorded. Inspect the available evidence before choosing a recovery action.';
  }
  const error = recovery?.error || state.detail.error || '';
  el['recovery-error'].textContent = error;
  el['recovery-error'].hidden = !error;
  const writable = state.detail.review_write_enabled;
  el['retry-execution'].hidden = !writable || !recovery?.can_retry;
  el['abandon-execution'].hidden = !writable || !recovery?.can_abandon;
  el['retry-execution'].disabled = state.busy;
  el['abandon-execution'].disabled = state.busy;
}

function feedbackCard(item, isDraft = false, addressedHere = false) {
  const card = document.createElement('article');
  card.className = `feedback-card${isDraft ? ' draft' : ''}${addressedHere ? ' resolved' : ''}`;
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
    status.addEventListener('click', () => {
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
  if (addressedHere && item.implementation_revision_id) {
    card.classList.add('has-location');
    card.title = 'Open the revision where this feedback was created';
    card.addEventListener('click', () => selectRevision(item.implementation_revision_id));
  }
  return card;
}

function renderFeedback() {
  const persisted = state.revision?.feedback || [];
  const items = [...persisted, ...state.drafts];
  const resolved = state.revision?.resolved_feedback || [];
  el['feedback-list'].replaceChildren(...items.map(
    (item, index) => feedbackCard(item, index >= persisted.length)
  ));
  el['empty-feedback'].hidden = items.length > 0;
  el['empty-feedback'].classList.toggle('compact', resolved.length > 0);
  el['empty-feedback-title'].textContent = !state.revision
    ? 'No implementation revision'
    : resolved.length ? 'No new feedback' : 'No feedback yet';
  el['empty-feedback-copy'].textContent = !state.revision
    ? 'Setup failed before code review. Use the recovery controls in the execution workspace.'
    : resolved.length
      ? 'Previously requested implementation changes are shown below.'
      : 'Review the patch and verification evidence first.';
  el['feedback-list'].hidden = items.length === 0;
  const openCount = persisted.filter((item) => item.status === 'OPEN').length;
  el['feedback-count'].textContent = String(openCount + state.drafts.length);
  el['resolved-feedback-section'].hidden = resolved.length === 0;
  el['resolved-feedback-count'].textContent = String(resolved.length);
  el['resolved-feedback-list'].replaceChildren(
    ...resolved.map((item) => feedbackCard(item, false, true))
  );
}

function openComposer() {
  el['feedback-section'].value = '';
  el['feedback-comment'].value = '';
  el.composer.hidden = false;
  el['feedback-section'].focus();
}

function closeComposer() {
  el.composer.hidden = true;
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
  });
  closeComposer();
  renderFeedback();
  renderActions();
}

function isCurrentRevision() {
  return Boolean(
    state.revision && state.revision.id === state.detail?.current_revision_id
  );
}

function renderActions() {
  const status = state.revision?.status;
  const current = isCurrentRevision();
  const writable = state.detail?.review_write_enabled && current;
  const review = writable && status === 'REVIEW';
  const changes = writable && status === 'CHANGES_REQUESTED';
  el['new-feedback'].hidden = !review;
  el.approve.hidden = !review;
  el['request-changes'].hidden = !review;
  el['address-agent'].hidden = !changes;
  el.approve.disabled = state.busy || state.drafts.length > 0;
  el['request-changes'].disabled = state.busy || state.drafts.length === 0;
  el['address-agent'].disabled = state.busy;

  if (!state.detail?.review_write_enabled) {
    el['decision-help'].textContent = 'Read-only deployment. Review mutations must be enabled on a trusted host.';
  } else if (!current) {
    el['decision-help'].textContent = state.detail.status === 'FAILED'
      ? 'Use the recovery controls after inspecting the preserved evidence.'
      : 'This historical implementation revision is read-only.';
  } else if (review) {
    el['decision-help'].textContent = state.drafts.length
      ? `${state.drafts.length} draft feedback item(s). Submit them together or remove them to approve.`
      : 'Approve only after independently checking the patch and deterministic evidence.';
  } else if (changes) {
    el['decision-help'].textContent = 'Ask the agent to revise the same worktree and rerun verification.';
  } else if (status === 'UPDATING') {
    el['decision-help'].textContent = 'The agent and deterministic checks are running. This page updates automatically.';
  } else if (status === 'FAILED') {
    el['decision-help'].textContent = 'Inspect the failure, then retry the exact operation or abandon this execution.';
  } else if (status === 'APPROVED') {
    el['decision-help'].textContent = 'This exact commit was approved. Enzo will verify the remote SHA before cleanup.';
  } else {
    el['decision-help'].textContent = 'This revision is not currently awaiting a human decision.';
  }
}

function renderHistory() {
  el['revision-history'].replaceChildren(...state.detail.implementation_revisions.map((revision) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `revision-link${revision.id === state.revision?.id ? ' current' : ''}`;
    button.textContent = `r${revision.revision_no}`;
    button.title = revision.status;
    button.addEventListener('click', () => selectRevision(revision.id));
    return button;
  }));
}

function reviewObjective() {
  if (!state.revision) return 'Implementation setup failed before a code revision was produced.';
  if (!isCurrentRevision()) return 'Inspecting an immutable historical implementation revision.';
  if (state.revision.status === 'UPDATING') return 'Enzo is producing and verifying this revision.';
  if (state.revision.status === 'CHANGES_REQUESTED') return 'Feedback is recorded for this exact commit.';
  if (state.revision.status === 'APPROVED') return 'This exact implementation revision was approved.';
  if (state.revision.status === 'FAILED') return 'The implementation attempt failed and remains inspectable.';
  return 'Confirm the code change satisfies the approved plan and verification contract.';
}

function render() {
  const detail = state.detail;
  const revision = state.revision;
  document.title = `Implementation r${revision?.revision_no || '—'} · Enzo`;
  el['feature-key'].textContent = detail.external_id;
  el['feature-title'].textContent = detail.title;
  el['reviewer-id'].textContent = detail.review_actor_id;
  el['implementation-title'].textContent = `Implementation · revision ${revision?.revision_no || '—'}`;
  el['status-chip'].textContent = (revision?.status || detail.status).replaceAll('_', ' ');
  el['status-chip'].className = `status-chip ${(revision?.status || detail.status).toLowerCase().replaceAll('_', '-')}`;
  el['head-sha'].textContent = revision?.head_sha ? `commit ${shortSha(revision.head_sha)}` : 'commit pending';
  el['execution-status'].textContent = detail.status;
  el.branch.textContent = detail.branch || 'pending';
  el['base-sha'].textContent = shortSha(detail.base_sha);
  el.agent.textContent = revisionAgentRun()?.runner?.name
    || revision?.created_by_id
    || 'pending';
  el['worktree-state'].textContent = detail.worktree_present
    ? 'Preserved'
    : ['CLOSED', 'CANCELLED'].includes(detail.status) ? 'Cleaned' : 'Pending';
  el['review-objective'].textContent = reviewObjective();
  el['review-guidance'].textContent = revision?.status === 'UPDATING'
    ? 'No human decision is required yet. This page updates automatically.'
    : !revision
      ? 'Inspect setup evidence before retrying or abandoning the execution.'
      : 'Review changed files, the full patch, and evidence before deciding.';
  renderStages();
  renderPatch();
  renderEvidence();
  renderAgentSummary();
  renderRecovery();
  renderFeedback();
  renderHistory();
  renderActions();
  setView(state.view);
}

function selectRevision(revisionId) {
  const revision = state.detail.implementation_revisions.find((item) => item.id === revisionId);
  if (!revision) return;
  const url = new URL(location.href);
  if (revision.id === state.detail.current_revision_id) url.searchParams.delete('revision');
  else url.searchParams.set('revision', revision.id);
  history.replaceState({}, '', url);
  state.revision = revision;
  state.drafts = [];
  closeComposer();
  render();
}

function setBusy(busy) {
  state.busy = busy;
  renderRecovery();
  renderActions();
}

async function submitAction(action, feedback = []) {
  setBusy(true);
  try {
    const payload = await api(`/api/implementation-revisions/${state.revision.id}/review-actions`, {
      method: 'POST',
      body: JSON.stringify({ action, feedback, request_id: requestId() }),
    });
    showToast(payload.event.message);
    state.drafts = [];
    const url = new URL(location.href);
    url.searchParams.delete('revision');
    history.replaceState({}, '', url);
    await load();
    return payload;
  } catch (error) {
    showToast(error.message, true);
    await load();
    return null;
  } finally {
    setBusy(false);
  }
}

async function submitRecovery(action) {
  setBusy(true);
  try {
    const payload = await api(`/api/executions/${executionId}/recovery-actions`, {
      method: 'POST',
      body: JSON.stringify({ action, request_id: requestId() }),
    });
    showToast(payload.event.message);
    await load();
    return payload;
  } catch (error) {
    showToast(error.message, true);
    await load();
    return null;
  } finally {
    setBusy(false);
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
      name: 'read_enzo_implementation_revision',
      title: 'Read Enzo implementation revision',
      description: 'Read the exact code patch, deterministic verification evidence, and structured feedback visible in Enzo.',
      inputSchema: { type: 'object', properties: {}, additionalProperties: false },
      annotations: { readOnlyHint: true, untrustedContentHint: true },
      execute: async () => ({
        execution_id: state.detail.id,
        revision: state.revision,
        evidence: currentEvidence(),
        branch: state.detail.branch,
        base_sha: state.detail.base_sha,
        head_sha: state.revision?.head_sha,
        recovery: state.detail.recovery,
      }),
    }, { signal: lifecycle.signal }),
    context.registerTool({
      name: 'submit_enzo_implementation_review',
      title: 'Submit Enzo implementation review',
      description: 'Approve the exact visible implementation revision, request changes with explicit feedback items, or ask the agent to address requested changes.',
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
        return { outcome: payload?.event?.outcome, message: payload?.event?.message };
      },
    }, { signal: lifecycle.signal }),
  ];
  for (const registration of registrations) Promise.resolve(registration).catch(report);
}

async function load() {
  clearTimeout(state.refreshTimer);
  state.refreshTimer = null;
  state.detail = await api(`/api/executions/${executionId}`);
  state.revision = chooseRevision(state.detail);
  render();
  registerWebMcpTools();
  const recoveryRunning = ['PENDING', 'RUNNING'].includes(state.detail.recovery?.status);
  if (
    state.detail.status === 'PENDING'
    || state.detail.status === 'RUNNING'
    || state.revision?.status === 'UPDATING'
    || recoveryRunning
  ) {
    state.refreshTimer = setTimeout(() => {
      load().catch((error) => showToast(error.message, true));
    }, 1800);
  }
}

for (const view of ['overview', 'patch', 'evidence']) {
  el[`view-${view}`].addEventListener('click', () => setView(view));
}
el['open-patch'].addEventListener('click', () => setView('patch'));
el['new-feedback'].addEventListener('click', openComposer);
el['cancel-comment'].addEventListener('click', closeComposer);
el['add-comment'].addEventListener('click', addDraft);
el.approve.addEventListener('click', () => submitAction('APPROVE'));
el['request-changes'].addEventListener('click', () => submitAction(
  'REQUEST_CHANGES',
  state.drafts.map(({ section, comment }) => ({ section, comment }))
));
el['address-agent'].addEventListener('click', () => submitAction('ADDRESS_WITH_AGENT'));
el['inspect-failure'].addEventListener('click', () => setView('evidence'));
el['retry-execution'].addEventListener('click', () => submitRecovery('RETRY'));
el['abandon-execution'].addEventListener('click', () => el['abandon-dialog'].showModal());
el['confirm-abandon'].addEventListener('click', async () => {
  el['abandon-dialog'].close();
  await submitRecovery('ABANDON');
});
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
  el['implementation-title'].textContent = 'Unable to load implementation';
});
