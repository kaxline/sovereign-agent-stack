// Save one draft in Mail.app (JXA). See docs/email.md.
//
// scripts/mail-drafts.py runs it with osascript in JavaScript mode and one
// JSON argument: {"command": "senders"} or {"command": "draft", "request":
// {...}, "open": bool}. It makes an outgoing message and saves it to Drafts;
// it never sends. It reads no message content: for a reply it only asks Mail
// to start a reply to the message with the given id. scripts/test-mail-drafts.py
// fails if this file gains a send, delete or move call, or reads content.

function run(argv) {
  const req = JSON.parse(argv[0]);
  const Mail = Application('Mail');
  if (req.command === 'senders') return JSON.stringify({addresses: senderAddresses(Mail)});
  if (req.command === 'draft') return JSON.stringify(draft(Mail, req.request, !!req.open));
  throw new Error('unknown command');
}

function senderAddresses(Mail) {
  const out = [];
  for (const list of Mail.accounts.emailAddresses()) {
    for (const addr of list) out.push(String(addr).toLowerCase());
  }
  return out;
}

function isMailbox(obj) {
  try { return obj.class() === 'mailbox'; } catch (e) { return false; }
}

// Same walk as mail_script.js: nested mailboxes are reached from their parent.
function findBox(Mail, accountId, path) {
  let account = null;
  for (const a of Mail.accounts()) {
    if (String(a.id()).toUpperCase() === accountId.toUpperCase()) account = a;
  }
  if (account === null) return null;
  const parts = path.split('/');
  let level = account.mailboxes();
  let box = null;
  for (let i = 0; i < parts.length; i++) {
    box = null;
    for (const candidate of level) {
      if (candidate.name() !== parts[i]) continue;
      if (i === 0) {
        let parent = null;
        try { parent = candidate.container(); } catch (e) { parent = null; }
        if (parent !== null && isMailbox(parent)) continue;
      }
      box = candidate;
      break;
    }
    if (box === null) return null;
    level = box.mailboxes();
  }
  return box;
}

function addRecipients(Mail, msg, request) {
  for (const address of request.to) msg.toRecipients.push(Mail.ToRecipient({address: address}));
  for (const address of request.cc) msg.ccRecipients.push(Mail.CcRecipient({address: address}));
  for (const address of request.bcc) msg.bccRecipients.push(Mail.BccRecipient({address: address}));
}

function draft(Mail, request, open) {
  if (request.from && senderAddresses(Mail).indexOf(request.from.toLowerCase()) < 0) {
    return {ok: false, reason: 'from_not_an_account'};
  }
  let msg;
  if (request.reply_to) {
    const box = findBox(Mail, request.reply_to.account, request.reply_to.mailbox);
    if (box === null) return {ok: false, reason: 'reply_mailbox_not_found'};
    const original = box.messages.byId(request.reply_to.id);
    try {
      original.id();
    } catch (e) {
      return {ok: false, reason: 'reply_message_not_found'};
    }
    msg = Mail.reply(original, {openingWindow: open, replyToAll: false});
    msg.content = request.body;
    if (request.subject) msg.subject = request.subject;
  } else {
    msg = Mail.OutgoingMessage({subject: request.subject, content: request.body, visible: open});
    Mail.outgoingMessages.push(msg);
  }
  addRecipients(Mail, msg, request);
  if (request.from) msg.sender = request.from;
  msg.save();
  return {ok: true};
}
