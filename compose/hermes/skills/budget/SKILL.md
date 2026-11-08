---
name: budget
description: "Look up and change the user's budget (Actual Budget): accounts, transactions, categories, payees, monthly budgets. Use for 'what did I spend on groceries this month', 'add a $12 coffee', 'how much is left in dining', 'move money between categories', or anything about spending, balances or budget categories. Uses the budget MCP tools (get-accounts, get-transactions, create-transaction and so on)."
version: 1.0.0
author: assistant stack (repo-shipped)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [budget, actual, finance, transactions, spending]
    category: productivity
---

# Budget (Actual Budget)

The `budget` MCP server holds one budget file. If none of its tools are in
your list, the budget isn't set up; say so rather than keeping figures
elsewhere.

## Look up ids first

Writes take ids, not names. Before creating or changing anything, resolve
each one with a read tool:

- `get-accounts` for account ids
- `get-grouped-categories` for category and category-group ids
- `get-payees` for payee ids

Never guess an id or reuse one from memory. If a name matches nothing, or
matches more than one, ask which is meant instead of creating a new category
or payee.

## Amounts are integer cents

Actual stores amounts as integer cents, negative for money out. A $12.50
coffee is `-1250`. Say amounts to the user in the budget's currency with a
decimal point (`$12.50`), never as raw cents, and check the sign before you
write.

## Confirm before these

State what will change, with ids resolved to names and amounts in currency,
and wait for a yes before:

- any delete (`delete-transaction`, `delete-category`, `delete-payee`, ...)
- a change to more than one transaction (`batch-update-transactions`,
  `import-transactions`, `merge-payees`)
- a move of money between categories. No single tool does it: it is two
  `set-budget-amount` calls, one down and one up. Read the month with
  `get-budget-month` first and say both figures.

A single new transaction the user just described needs no extra
confirmation.

## Leave to the user

Reconciling accounts and syncing with a bank are the user's to do in the
Actual app. Don't try to mark transactions cleared to make a balance match,
and don't look for a bank-sync tool.

## Reads and unattended runs

Reads sync with the server first, but a read within about 5 seconds of a
write may miss it. If a figure looks stale right after a change, wait and
read again.

On scheduled and API runs, and in chats the user limited to looking things
up, the write tools are missing. Report what you found and what you would
change; don't retry a write through another tool.
