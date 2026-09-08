# unsaltedbutter.ai — home page + `/install` route (Next.js)

Drop-in files for the marketing/home page and the install-script route on
`https://unsaltedbutter.ai/`.

Assumes your project at `~/unsaltedbutter/web` is an **app-router** Next.js
project on **Next 16 / React 19**, running as server components. **No new
dependencies** — only `react` and `next` are imported.

## File mapping

From this `website/` dir, copy into your `~/unsaltedbutter/web`:

| This file | Goes to (`src/app`) | Or to (`app`) |
| --------- | ------------------- | ------------- |
| `app/page.tsx` | `src/app/page.tsx` | `app/page.tsx` |
| `app/page.module.css` | `src/app/page.module.css` | `app/page.module.css` |
| `app/copy-button.tsx` | `src/app/copy-button.tsx` | `app/copy-button.tsx` |
| `app/install/route.ts` | `src/app/install/route.ts` | `app/install/route.ts` |

If your project uses `src/app`, drop the files under `src/app/` (paths above
assume `app/` at the project root; adjust accordingly). `page.module.css` is
the co-located stylesheet and is required — both `page.tsx` and
`copy-button.tsx` import it.

Nothing else changes. The home page becomes `/` and the script becomes
`/install`. `page.tsx` exports static `metadata` for SEO/social previews.

## How the routes behave

- **`/`** — static server component rendering the marketing copy, the
  copy-paste install block, and the GitHub link. No client JS on first paint
  except the tiny copy button.
- **`/install`** — a `GET` route handler that **302-redirects** to
  `https://raw.githubusercontent.com/unsaltedbutter-ai/local-wallet/main/install.sh`
  with a short `Cache-Control: public, max-age=300` header. So
  `curl -fsSL https://unsaltedbutter.ai/install | bash` resolves to the real
  script from the repo.

  **Trade-off, documented:** a redirect means the served script is whatever is
  at `main` at request time, plus one extra round-trip. We chose this over
  streaming/fetching inside the route for zero dependencies and simplicity.
  If you want an immutable snapshot, download `install.sh` into `public/` and
  serve it directly from `/install` instead.

## Deploy

Your app runs under PM2 (cluster, `:3000`) behind nginx. Do **not** restart
services just to add these files — a Next.js build redeploy picks them up.

1. Copy the files per the mapping above.
2. Build & deploy as you normally do, e.g.:
   ```bash
   cd ~/unsaltedbutter/web
   npm run build
   pm2 restart <your-app-name>      # or: pm2 reload <name> --update-env
   ```
3. nginx already passes `/` through
   `proxy_pass http://127.0.0.1:3000` for the site; that same block covers
   `/install` and `/`. No nginx change is required.
   - **Optional long-cache rule for `/install`:** only if you switch to
     serving an immutable snapshot from `public/`. With the redirect approach
     leave it alone — a long cache on a 302 would pin stale redirects.
   - If your nginx only proxied specific locations before, ensure both `/`
     and `/install` are covered by a location that proxies to `127.0.0.1:3000`.

## Verify

1. `curl -fsSL https://unsaltedbutter.ai/install | bash` downloads the
   install script (shows a script, not a 404 or an HTML page).
2. Open `https://unsaltedbutter.ai/` — the page renders, is legible in both
   light and dark mode, and the GitHub link works.
3. Click the **Copy** button in the install block — it copies the command and
   the label flips to "Copied".

## Notes / caveats

- Dark-mode friendly via `prefers-color-scheme`; design tokens live in
  CSS custom properties at the top of `page.module.css`.
- The `Copy` button uses the `navigator.clipboard` API; on non-HTTPS or
  blocked-permission contexts it silently no-ops and the block is still
  manually selectable text.
- Copy is deliberately restrained, developer-credible, and makes **no
  invented claims** — it states watch-only (xpubs only), local LLM intent
  envelopes, deterministic money logic, hardware signing, mainnet-only, and
  the honest public-Esplora privacy trade-off.
