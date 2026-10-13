// A fake Mail.app scripting model, for testing mail_script.js and
// mail_drafts.js under Node without touching real mail. Tests only.
//
//   node fake_mail_app.js <fixture.json> <script.js> '<request json>'
//
// The fixture comes from synthetic_mail.script_fixture(). It mimics the parts
// of JXA the reader uses: element collections that are callable, answer
// batch property calls (`messages.subject()` returns every subject), support
// `.length` and `.byId()`, and objects whose properties are methods. Errors
// are printed the way osascript prints them, ending in an Apple event code.
//
// Fixture options:
//   deny: true             every call fails with -1743 (Automation not allowed)
//   flatten: true          account.mailboxes lists nested mailboxes too, as Mail can
//   arrive: {account, path, message}
//                          add a message to that mailbox after the first
//                          dateReceived batch, to check the reader re-reads
//   drafts_out: path       each saved outgoing message is appended there as
//                          a JSON line; there is no way to send one
//   fail_save: true        saving an outgoing message fails with -10000

'use strict';

const fs = require('fs');
const vm = require('vm');

function appleError(text, code) {
  const err = new Error(`${text} (${code})`);
  err.appleCode = code;
  return err;
}

function missing() {
  return new Proxy({}, {
    get() { return function () { throw appleError("Can't get object.", -1728); }; },
  });
}

// items is live: later pushes are visible to later calls.
// onBatch(prop) runs after each batch property call.
function collection(items, key, onBatch) {
  const f = function () { return items.slice(); };
  return new Proxy(f, {
    apply() { return items.slice(); },
    get(target, prop) {
      if (prop === 'length') return items.length;
      if (prop === 'byId') {
        return function (id) { return items.find(function (i) { return i[key]() === id; }) || missing(); };
      }
      if (typeof prop !== 'string') return undefined;
      if (/^[0-9]+$/.test(prop)) return items[Number(prop)] || missing();
      return function () {
        const out = items.map(function (item) {
          if (typeof item[prop] !== 'function') throw appleError("Can't get property.", -1728);
          return item[prop]();
        });
        if (onBatch) onBatch(prop);
        return out;
      };
    },
  });
}

function buildMessage(m) {
  return {
    id: () => m.id,
    dateReceived: () => new Date(m.received * 1000),
    dateSent: () => new Date(m.sent * 1000),
    subject: () => m.subject,
    sender: () => m.sender,
    readStatus: () => m.read,
    flaggedStatus: () => m.flagged,
    deletedStatus: () => m.deleted,
    messageId: () => m.message_id.replace(/^<|>$/g, ''),
    allHeaders: () => {
      // null stands for a message Mail lists but can no longer produce.
      if (m.headers === null) throw appleError("Can't get object.", -1728);
      return m.headers;
    },
    content: () => m.content,
    mailAttachments: collection(m.attachments.map(function (a) {
      return {name: () => a.name, mimeType: () => a.mime, fileSize: () => a.size};
    }), 'name'),
  };
}

function recipients() {
  const items = [];
  return {items: items, push(r) { items.push(r.address); }};
}

// An outgoing message as JXA makes it: plain settable properties, recipient
// lists that take pushes, and save(). Saving appends it to drafts_out.
function outgoing(fixture, outgoingList, props, reply) {
  const msg = {
    subject: props.subject || '', content: props.content || '', visible: !!props.visible, sender: null,
    toRecipients: recipients(), ccRecipients: recipients(), bccRecipients: recipients(),
    save() {
      if (fixture.fail_save) throw appleError('Mail got an error saving the message.', -10000);
      if (!reply && outgoingList.indexOf(msg) < 0) throw appleError("Can't save a message that wasn't made.", -10000);
      fs.appendFileSync(fixture.drafts_out, JSON.stringify({
        subject: msg.subject, content: msg.content, visible: msg.visible, sender: msg.sender,
        to: (reply ? reply.to : []).concat(msg.toRecipients.items), cc: msg.ccRecipients.items,
        bcc: msg.bccRecipients.items, reply_to: reply ? reply.id : null,
        reply_all: reply ? reply.all : null,
      }) + '\n');
    },
  };
  return msg;
}

function buildApp(fixture) {
  const accounts = [];
  for (const a of fixture.accounts) {
    const account = {id: () => a.id, name: () => a.name, class: () => 'imapAccount',
                     emailAddresses: () => (a.email_addresses || []).slice()};
    const all = [];
    function buildBox(node, parent, prefix) {
      const path = prefix ? prefix + '/' + node.name : node.name;
      const messages = node.messages.map(buildMessage);
      let fired = false;
      function onBatch(prop) {
        const arrive = fixture.arrive;
        if (prop === 'dateReceived' && !fired && arrive && arrive.account === a.id && arrive.path === path) {
          fired = true;
          messages.unshift(buildMessage(arrive.message));
        }
      }
      const box = {
        name: () => node.name,
        class: () => 'mailbox',
        container: () => parent,
        messages: collection(messages, 'id', onBatch),
      };
      all.push(box);
      box.mailboxes = collection(node.children.map(function (c) { return buildBox(c, box, path); }), 'name');
      return box;
    }
    const top = a.mailboxes.map(function (n) { return buildBox(n, account, ''); });
    account.mailboxes = collection(fixture.flatten ? all : top, 'name');
    accounts.push(account);
  }
  const outgoingList = [];
  return {
    accounts: collection(accounts, 'id'),
    outgoingMessages: {push(m) { outgoingList.push(m); }},
    OutgoingMessage(props) { return outgoing(fixture, outgoingList, props || {}, null); },
    ToRecipient(props) { return {address: props.address}; },
    CcRecipient(props) { return {address: props.address}; },
    BccRecipient(props) { return {address: props.address}; },
    reply(original, opts) {
      const id = original.id();
      const sender = /<([^>]+)>/.exec(original.sender());
      const subject = original.subject();
      return outgoing(fixture, outgoingList, {
        subject: /^re:/i.test(subject) ? subject : 'Re: ' + subject,
        content: '\n\n> (quoted original)', visible: !!(opts && opts.openingWindow),
      }, {id: id, all: !!(opts && opts.replyToAll), to: sender ? [sender[1]] : []});
    },
  };
}

function main() {
  const [fixturePath, scriptPath, request] = process.argv.slice(2);
  const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
  const app = buildApp(fixture);
  const context = {
    Application(name) {
      if (name !== 'Mail') throw appleError('Application can\'t be found.', -2700);
      if (fixture.deny) throw appleError('Not authorized to send Apple events to Mail.', -1743);
      return app;
    },
    JSON,
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(scriptPath, 'utf8'), context, {filename: scriptPath});
  try {
    process.stdout.write(String(context.run([request])) + '\n');
  } catch (err) {
    process.stderr.write(`execution error: Error: ${err.message}\n`);
    process.exit(1);
  }
}

main();
