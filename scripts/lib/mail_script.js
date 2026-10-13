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

// [{path, box}] for one account. Mail may list nested mailboxes at the top
// level as well as under their parent, so a top-level entry whose container
// is a mailbox is skipped and reached through its parent instead.
function mailboxTree(account) {
  const out = [];
  const seen = {};
  function walk(boxes, prefix, top) {
    for (const box of boxes) {
      if (top) {
        let parent = null;
        try { parent = box.container(); } catch (e) { parent = null; }
        if (parent !== null && isMailbox(parent)) continue;
      }
      const path = prefix ? prefix + '/' + box.name() : box.name();
      if (seen[path]) continue;
      seen[path] = true;
      out.push({path: path, box: box});
      walk(box.mailboxes(), path, false);
    }
  }
  walk(account.mailboxes(), '', true);
  return out;
}

function accountsById(Mail) {
  const byId = {};
  for (const account of Mail.accounts()) {
    byId[String(account.id()).toUpperCase()] = account;
  }
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

function findBox(byId, accountId, path) {
  const account = byId[accountId.toUpperCase()];
  if (!account) return null;
  for (const entry of mailboxTree(account)) {
    if (entry.path === path) return entry.box;
  }
  return null;
}

// Summaries of every message in each requested mailbox received at or after
// req.since (Unix seconds). Properties are fetched for the whole mailbox at
// once, one Apple event per property, which is much faster than asking each
// message in turn or using a `whose` clause.
function listMessages(Mail, req) {
  const byId = accountsById(Mail);
  const out = [];
  for (const want of req.boxes) {
    const box = findBox(byId, want.account, want.path);
    if (!box) {
      out.push({account: want.account, path: want.path, found: false, messages: []});
      continue;
    }
    out.push({account: want.account, path: want.path, found: true, messages: summaries(box, req.since)});
  }
  return {boxes: out};
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
  const box = findBox(byId, req.account, req.path);
  if (!box) return {found: false, messages: []};
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
