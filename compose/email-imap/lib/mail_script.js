// Read-only Mail.app reader for the mail indexer (JXA). See docs/email.md.
//
// scripts/lib/mail_script.py runs it with osascript in JavaScript mode and
// one JSON argument: {"command": "mailboxes" | "list" | "details", ...}. It
// prints one JSON value. It only reads properties: there is no send, move,
// delete, flag or compose code here, and scripts/test-mail-script.py fails if
// any appears. Error output names a category, never message content.

function run(argv) {
  const req = JSON.parse(argv[0]);
  const Mail = Application('Mail');
  if (req.command === 'mailboxes') return JSON.stringify(listMailboxes(Mail, req));
  if (req.command === 'list') return JSON.stringify(listMessages(Mail, req));
  if (req.command === 'details') return JSON.stringify(details(Mail, req));
  throw new Error('unknown command');
}

function isMailbox(obj) {
  try { return obj.class() === 'mailbox'; } catch (e) { return false; }
}

// Mail may list nested mailboxes at the top level as well as under their
// parent. A top-level entry whose container is a mailbox is one of those.
function nestedAtTop(box) {
  let parent = null;
  try { parent = box.container(); } catch (e) { parent = null; }
  return parent !== null && isMailbox(parent);
}

// [{path, box}] for every mailbox in one account. Names come one level at a
// time (one Apple event per level), not one mailbox at a time. Only the
// `mailboxes` command and wildcard patterns need the whole tree.
function mailboxTree(account) {
  const out = [];
  const seen = {};
  function walk(level, prefix, top) {
    const names = level.name();
    const boxes = level();
    for (let i = 0; i < boxes.length; i++) {
      if (top && nestedAtTop(boxes[i])) continue;
      const path = prefix ? prefix + '/' + names[i] : names[i];
      if (seen[path]) continue;
      seen[path] = true;
      out.push({path: path, box: boxes[i]});
      walk(boxes[i].mailboxes, path, false);
    }
  }
  walk(account.mailboxes, '', true);
  return out;
}

// Accounts by upper-case id; all ids come in one Apple event.
function accountsById(Mail) {
  const byId = {};
  const ids = Mail.accounts.id();
  const accounts = Mail.accounts();
  for (let i = 0; i < ids.length; i++) byId[String(ids[i]).toUpperCase()] = accounts[i];
  return byId;
}

function epoch(d) {
  return d ? Math.floor(d.getTime() / 1000) : null;
}

// {accounts: [{id, name, mailboxes: [{path, count?}]}]}
// req.accounts limits the listing to those ids; req.counts adds message counts.
function listMailboxes(Mail, req) {
  const byId = accountsById(Mail);
  const wanted = req.accounts ? req.accounts.map(function (a) { return a.toUpperCase(); }) : Object.keys(byId);
  const out = [];
  for (const id of wanted) {
    const account = byId[id];
    if (!account) continue;
    const boxes = mailboxTree(account).map(function (entry) {
      const item = {path: entry.path};
      if (req.counts) item.count = entry.box.messages.length;
      return item;
    });
    out.push({id: String(account.id()), name: account.name(), mailboxes: boxes});
  }
  return {accounts: out};
}

// {box, path} for one mailbox, walking down its path and reading only the
// names on the way (one Apple event per level), or null. Names match exactly
// when they can, else ignoring case; `path` is Mail's own spelling.
function findBox(byId, accountId, path) {
  const account = byId[accountId.toUpperCase()];
  if (!account) return null;
  let level = account.mailboxes;
  let box = null;
  const found = [];
  const parts = path.split('/');
  for (let d = 0; d < parts.length; d++) {
    const names = level.name();
    const boxes = level();
    let hits = [];
    for (let i = 0; i < names.length; i++) if (names[i] === parts[d]) hits.push(i);
    if (hits.length === 0) {
      for (let i = 0; i < names.length; i++) {
        if (String(names[i]).toLowerCase() === parts[d].toLowerCase()) hits.push(i);
      }
    }
    if (d === 0 && hits.length > 1) hits = hits.filter(function (i) { return !nestedAtTop(boxes[i]); });
    if (hits.length === 0) return null;
    box = boxes[hits[0]];
    found.push(names[hits[0]]);
    level = box.mailboxes;
  }
  return {box: box, path: found.join('/')};
}

// Summaries of every message in each requested mailbox received at or after
// req.since (Unix seconds). Properties are fetched for the whole mailbox at
// once, one Apple event per property, which is much faster than asking each
// message in turn or using a `whose` clause.
function listMessages(Mail, req) {
  const byId = accountsById(Mail);
  const out = [];
  const missing = {};
  for (const want of req.boxes) {
    if (!byId[want.account.toUpperCase()]) missing[want.account] = true;
    const hit = findBox(byId, want.account, want.path);
    if (!hit) {
      out.push({account: want.account, path: want.path, found: false, messages: []});
      continue;
    }
    out.push({account: want.account, path: hit.path, found: true, messages: summaries(hit.box, req.since)});
  }
  return {boxes: out, accounts_missing: Object.keys(missing)};
}

function summaries(box, since) {
  // The arrays below come from separate Apple events. If mail arrives or
  // leaves between them they would no longer line up, so re-read the ids at
  // the end and start over when they changed.
  for (let attempt = 0; attempt < 3; attempt++) {
    const m = box.messages;
    const ids = m.id();
    if (ids.length === 0) return [];
    const received = m.dateReceived();
    const keep = [];
    for (let i = 0; i < ids.length; i++) {
      const t = epoch(received[i]);
      if (t !== null && t >= since) keep.push(i);
    }
    if (keep.length === 0) return [];
    const sent = m.dateSent();
    const subject = m.subject();
    const sender = m.sender();
    const read = m.readStatus();
    const flagged = m.flaggedStatus();
    const deleted = m.deletedStatus();
    const messageId = m.messageId();
    const after = m.id();
    if (after.length !== ids.length || after.some(function (v, i) { return v !== ids[i]; })) continue;
    return keep.map(function (i) {
      return {
        id: ids[i], received: epoch(received[i]), sent: epoch(sent[i]),
        subject: subject[i], sender: sender[i], read: !!read[i], flagged: !!flagged[i],
        deleted: !!deleted[i], message_id: messageId[i],
      };
    });
  }
  throw new Error('mailbox kept changing while it was read');
}

// Headers, and optionally the plain-text body and attachment names, for the
// given message ids in one mailbox. Attachment contents are never read.
function details(Mail, req) {
  const byId = accountsById(Mail);
  const hit = findBox(byId, req.account, req.path);
  if (!hit) return {found: false, messages: []};
  const box = hit.box;
  const out = [];
  for (const id of req.ids) {
    const msg = box.messages.byId(id);
    const item = {id: id};
    try {
      item.headers = String(msg.allHeaders() || '').slice(0, 65536);
    } catch (e) {
      out.push({id: id, error: 'missing'});
      continue;
    }
    try {
      if (req.body) item.content = String(msg.content() || '').slice(0, req.max_chars);
      if (req.attachments) {
        const atts = msg.mailAttachments;
        const names = atts.name();
        const types = atts.mimeType();
        const sizes = atts.fileSize();
        item.attachments = names.map(function (name, i) {
          return {name: name, mime: types[i], size: sizes[i]};
        });
      }
    } catch (e) {
      out.push({id: id, error: 'unreadable'});
      continue;
    }
    out.push(item);
  }
  return {found: true, messages: out};
}
