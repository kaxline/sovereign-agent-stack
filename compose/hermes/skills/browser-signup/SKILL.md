---
name: browser-signup
description: "Create an account on a website in the local browser, and capture an API key it shows afterwards. Use for 'sign me up for…', 'create an account on…', 'register me at…', 'get me an API key from…', or any page with a sign-up form (new-password fields). Covers what to ask the user first, the browser_vault create_login and capture_secret actions, verification codes and links, and where to stop."
version: 1.0.0
author: assistant stack (repo-shipped)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [browser, signup, register, account, password, api-key, vault]
    category: productivity
---

# Browser sign-up

You create the account; Hermes creates and stores the password. You never see,
type, choose or ask for a password, and you never read an API key off a page.
Both go through the `browser_vault` tool:

- `action="create_login"` fills a sign-up form's password fields with a
  password Hermes generates once the user approves, and saves the login.
- `action="capture_secret"` stores a key the page shows, after the user
  approves, without the value reaching you.

Signing in to an account that already exists is the `browser-login` skill.

## Driving the page

Every page action is Python in `browser_exec`. The helpers (`new_tab`,
`goto_url`, `wait_for_load`, `page_info`, `js`, `fill_input`) are already
defined. Do not import them. Only `print(...)` output comes back.

## Steps

1. **Ask first, before opening anything.** Ask the user which email address or
   username to register, and for any profile fields sites usually want (name,
   company). If their message already gives them, do not ask again. Never ask
   for a password, and do not suggest one.
2. **Open the sign-up page** and list the form's inputs, so you type into
   fields that exist:
   ```python
   new_tab("https://example.com/signup")
   wait_for_load()
   print(page_info())
   print(js("JSON.stringify([...document.querySelectorAll('input, select')].map(i => ({type: i.type, name: i.name, id: i.id, autocomplete: i.autocomplete, required: i.required, label: (i.labels && i.labels[0] ? i.labels[0].textContent.trim() : '')})))"))
   ```
   A field the form requires that the user hasn't given you (a phone number,
   say): ask for it. Do not make one up.
3. **Type the identifier and profile fields** with `fill_input`. Leave the
   password fields alone.
4. **Terms and privacy checkboxes.** Ask the user before ticking one, quoting
   its text and link text, for example: "The form asks you to agree to the
   'Terms of Service' and 'Privacy Policy'. Shall I tick it?" Tick it only on a
   yes. Leave marketing opt-ins unticked unless they ask.
5. **Create the login** once the password fields are on screen:
   `browser_vault(action="create_login", identifier="<what the user chose>")`.
   Hermes fills every password field, including the confirm field. If the form
   asks for the password on a later step, go to that step first.
6. **Submit** with a `js(...)` click, then `print(page_info())` to see where
   the site went.
7. **Verification.**
   - A code sent by email or text: `browser_vault(action="enter_code",
     handle="<handle from create_login>")`. The user types it in a prompt.
   - A link sent by email: ask the user to open it and to tell you when it is
     done. Then reload the page.
8. **Tell the user the account exists**, with the site and the identifier.
   Go on to anything after sign-up (an API key, a plan, settings) only if
   their request asked for it or they say yes now.

## API keys

1. Open the dashboard's key page and create the key as the user asked.
2. As soon as the site shows it, call
   `browser_vault(action="capture_secret", name="<VAR_NAME>", destination="env" | "viewer")`.
   Use the name and destination the request gives. Without one, ask: `env`
   puts it in this profile's `.env` for a tool or skill here to use; `viewer`
   hands it to the user's app. Names look like `PREM_API_KEY`.
3. If it reports several candidates, choose by where each one is and its last
   four characters, and call again with `candidate`. If it finds none and the
   site has a reveal or show button, click that and call again.
4. Close the dialog or leave the page before reading anything else on it.

## Tool results

| Result | What to do |
|---|---|
| `prompt_unavailable` | This session cannot show the user a prompt. Tell them to create the account in their own browser; you can sign in later with `browser-login`. For a key, tell them where it is so they paste it into the app. Stop. |
| `save_declined` | Stop asking this turn. Say they can retry. |
| `no_new_password_field` | The password step isn't on screen. Move the form on, or open the sign-up form rather than a sign-in page. |
| `password_rules_unmet` | Call `create_login` again; the user can type a password of their own in the card. |
| The site rejects the password after submit | Call `create_login` again with the `handle` it returned. It replaces the password rather than adding a second login. |
| `several_candidates` | Pick by `where` and `last4`, then call `capture_secret` with `candidate`. |
| `no_secret_found` | Reveal the key if the page hides it; otherwise tell the user where it is. Do not print the page to look for it. |

## Where to stop

- **A CAPTCHA** ("I'm not a robot", picture puzzles, "verify you are human",
  hCaptcha, Turnstile): stop and tell the user the site wants a human check.
  Never try to solve it, click through it, wait it out or use another
  service to get past it. They can finish the sign-up in their own browser.
- **Payment details** on the sign-up form: stop and ask. A card goes in only
  through the vault's own fill, after the user confirms.
- **Identity documents** or other details the user hasn't given you: ask, do
  not invent them.

## Pitfalls

- **`save_login` on a sign-up form.** That asks the user for an existing
  password. Use `create_login`.
- **Typing a password with `fill_input` or `js(...)`**, even one the user
  wrote in chat. Tell them it was not used, and use `create_login`.
- **Printing page text while a key is on screen** (`document.body.innerText`,
  the key's input, a screenshot) to find it. Call `capture_secret` instead.
- **Ticking terms boxes without asking.**
- **Opening the page before asking for the identifier.** Ask first.
