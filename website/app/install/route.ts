import { NextResponse } from "next/server";

// Serves the install script. We 302-redirect to the pinned raw GitHub URL:
// zero deps, no streaming/edge concerns, and the user can review the exact
// script at the URL before running it. Trade-off vs streaming: a redirect is
// one extra round-trip and the served script is whatever is at `main` right
// now, rather than a snapshot we host ourselves. Host a copy in /public and
// serve it directly if you want immutability; until then this is the simplest
// thing that satisfies "curl -fsSL ... | bash".
export function GET() {
  return NextResponse.redirect(
    "https://raw.githubusercontent.com/unsaltedbutter-ai/local-wallet/main/install.sh",
    {
      status: 302,
      headers: {
        "Cache-Control": "public, max-age=300",
      },
    },
  );
}
