---
name: browser-login
description: "Sign in to a website in the local browser. Use for 'log me into…', 'sign in to…', 'save my login for…', or any page that shows a username/password form. Covers the order of the browser_vault_* tools and why the sign-in page must be open first."
version: 1.0.0
author: assistant stack (repo-shipped)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [browser, login, sign-in, password, vault]
    category: productivity
---

# Browser login

Passwords reach a page only through the `browser_vault_*` tools. You never see,
type, ask for, or accept a password, even if the user pastes one.

The vault binds every login to the **origin of the page that is open**. With no
page open there is no origin, so `browser_vault_save_login` and
`browser_vault_fill` cannot work yet. Open the page first.

## When to use this skill

- "Log me into X" / "sign in to X" / "use my account on X"
- "Save my login for X"
- A task reaches a page with a password field

## Steps

1. **Open the sign-in page.** `browser_navigate` to the site's login URL
   (`browser_exec` on the browser-use backend). If you only know the home page,
   open it and click through to "Sign in". Confirm a password field is on the
   page (`browser_snapshot`) before going on.
2. **Look for a saved login.** `browser_vault_list`. If it reports a locked
   backend, `browser_vault_unlock` with that backend's name.
3. **Fill or save.**
   - A login exists for this origin: `browser_vault_fill` with its handle.
   - None exists: `browser_vault_save_login` (optional `label`). The user enters
     the login in a masked prompt; Hermes stores it and fills the password.
4. **Finish the form.** Type the identifier the tool returned into the username
   field if the form has one, then submit (`browser_click` / `browser_press`).
5. **One-time code.** If the site then asks for a code, call
   `browser_vault_enter_code` with the handle you just used.

## Tool results

| Result | What to do |
|---|---|
| "Open the site's login page first" | Step 1 was skipped. Navigate, then retry. |
| `prompt_unavailable` | This session cannot show the prompt (API, cron). Tell the user to run `hermes vault add` or use Settings → Passwords & Logins for that site. Stop. |
| `save_declined` | Stop asking this turn. Say they can retry, or add it later in Settings. |

## Pitfalls

- **Calling `browser_vault_save_login` before any page is open.** It fails, and
  is the most common mistake.
- **Asking for the password in chat.** Never. Not "what's your password?", not
  "paste it here". Call `browser_vault_save_login` instead.
- **Typing a password with `browser_type`.** Never, even one the user wrote in
  chat. Tell them it was not used and route through the vault.
- **Navigating, then stopping to ask the user how to proceed.** If the sign-in
  page is open and nothing is saved, call `browser_vault_save_login`. Do not
  stop to ask first.
