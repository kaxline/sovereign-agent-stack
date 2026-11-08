---
name: browser-login
description: "Sign in to a website in the local browser. Use for 'log me into…', 'sign in to…', 'save my login for…', or any page that shows a username/password form. Covers the order of the browser_vault actions and why the sign-in page must be open first."
version: 1.5.0
author: assistant stack (repo-shipped)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [browser, login, sign-in, password, vault]
    category: productivity
---

# Browser login

Passwords reach a page only through the `browser_vault` tool. You never see,
type, ask for, or accept a password, even if the user pastes one.

The vault binds every login to the **origin of the page that is open**. With no
page open there is no origin, so `save_login` and `fill` cannot work yet.
Open the page first.

`browser_vault` takes an `action` (`list`, `fill`, `save_login`, `enter_code`,
`unlock`) plus that action's arguments, for example
`browser_vault(action="fill", handle="vault_…")`. There are no separate
`browser_vault_fill` or `browser_vault_save_login` tools.

## When to use this skill

- "Log me into X" / "sign in to X" / "use my account on X"
- "Save my login for X"
- A task reaches a page with a password field

Creating a new account is the `browser-signup` skill.

## Driving the page

Every page action is Python in `browser_exec`. The helpers (`new_tab`,
`goto_url`, `wait_for_load`, `page_info`, `js`, `fill_input`) are already
defined. Do not import them. Only `print(...)` output comes back.

## Steps

1. **Open the sign-in page** at the site's login URL, not its home page. If you
   only know the home page, open it and click through to "Sign in". Then list
   the form's inputs, so you type into fields that exist instead of guessing
   selectors like `#username`.
   ```python
   # Opening the sign-in page
   new_tab("https://www.linkedin.com/login")
   wait_for_load()
   print(page_info())
   print(js("JSON.stringify([...document.querySelectorAll('input')].map(i => ({type: i.type, name: i.name, id: i.id, autocomplete: i.autocomplete})))"))
   ```
   A password input in that list means the form is open.
2. **Look for a saved login.** `browser_vault` with `action="list"`. If it
   reports a locked backend, call `action="unlock"` with `backend` set to that
   backend's name.
3. **Fill or save.**
   - A login exists for this origin: `action="fill"` with its `handle`.
   - None exists: `action="save_login"` (optional `label`). The user enters
     the login in a masked prompt; Hermes stores it and fills the password.
   - The save result's `fill.success` is false: the login **is** saved, but the
     page had no form. Open the sign-in form, then `action="fill"` with the
     `handle` from the save result. Do not save again.
4. **Finish the form.** Type the identifier the tool returned into the username
   field from step 1's list, if the form has one, then submit with
   `fill_input` and a `js(...)` click in `browser_exec`.
5. **One-time code.** If the site then asks for a code, call
   `action="enter_code"` with the handle you just used.

## Tool results

| Result | What to do |
|---|---|
| "Open the site's login page first" | Step 1 was skipped. Navigate, then retry. |
| `prompt_unavailable` | This session cannot show the prompt (API, cron). Tell the user to run `hermes vault add` or use Settings → Passwords & Logins for that site. Stop. |
| `save_declined` | Stop asking this turn. Say they can retry, or add it later in Settings. |

## Pitfalls

- **`from browser_helpers import …`** or any other import of the helpers. That
  module does not exist; the call fails. Call `new_tab(...)` directly.
- **`page_info()` without `print`.** The result comes back empty and tells you
  nothing. Print everything you need to read.
- **Calling `save_login` before any page is open.** It fails, and
  is the most common mistake.
- **Saving from a page without the form** (a home page). The login is stored
  but nothing is filled. Follow the result's `next`: open the form, then
  `action="fill"` with the handle.
- **Saving a login on a sign-up form.** A form that asks for a new password
  (create, confirm) is a sign-up. Follow the `browser-signup` skill, which uses
  `action="create_login"`, not `save_login`.
- **Asking for the password in chat.** Never. Not "what's your password?", not
  "paste it here". Call `browser_vault` with `action="save_login"` instead.
- **Typing a password with `fill_input` or `js(...)`.** Never, even one the
  user wrote in chat. Tell them it was not used and route through the vault.
- **Navigating, then stopping to ask the user how to proceed.** If the sign-in
  page is open and nothing is saved, call `action="save_login"`. Do not
  stop to ask first.
