// A fake Mail.app scripting model, for testing mail_script.js under Node
// without touching real mail. Tests only.
//
//   node fake_mail_app.js <fixture.json> <mail_script.js> '<request json>'
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

function buildApp(fixture) {
  const accounts = [];
  for (const a of fixture.accounts) {
    const account = {id: () => a.id, name: () => a.name, class: () => 'imapAccount'};
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
  return {accounts: collection(accounts, 'id')};
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
