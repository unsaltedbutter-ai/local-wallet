"use client";

import { useState } from "react";
import styles from "./page.module.css";

const TEXT = "curl -fsSL https://unsaltedbutter.ai/install | bash";

export default function CopyButton() {
  const [copied, setCopied] = useState(false);

  async function onCopy() {
    try {
      await navigator.clipboard.writeText(TEXT);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard API can be blocked (insecure context, permissions). The
      // block is still selectable text, so manual copy works.
      setCopied(false);
    }
  }

  return (
    <button
      type="button"
      onClick={onCopy}
      aria-live="polite"
      className={styles.copy}
    >
      {copied ? "Copied" : "Copy"}
    </button>
  );
}
