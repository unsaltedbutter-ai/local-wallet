import styles from "./page.module.css";
import CopyButton from "./copy-button";

export const metadata = {
  title: "unsaltedbutter — a watch-only, local-first Bitcoin wallet",
  description:
    "A watch-only, local-first Bitcoin wallet. Your keys never touch the app; a local LLM turns plain English into strictly validated intent envelopes; money logic is deterministic code.",
};

export default function Home() {
  return (
    <div className={styles.page}>
      <header className={styles.mast}>
        <span className={styles.wordmark}>unsaltedbutter</span>
        <span className={styles.tag}>local-first Bitcoin wallet</span>
      </header>

      <main className={styles.content}>
        <h1>Your wallet, on your machine, keys in your hands.</h1>

        <p className={styles.lede}>
          unsaltedbutter is a <strong>watch-only</strong>, local-first Bitcoin
          wallet. It watches your addresses, helps you construct and broadcast
          transactions, and never touches your keys.
        </p>

        <section aria-labelledby="how">
          <h2 id="how">How it works</h2>
          <ul className={styles.facts}>
            <li>
              <strong>Watch-only by design.</strong> The app only ever holds{" "}
              <em>xpubs</em> — extended public keys. Private keys and seed
              phrases never enter the app, its disk, or its logs. They stay in
              your hardware wallet.
            </li>
            <li>
              <strong>Local LLM, intent envelopes.</strong> A model runs on
              your machine and turns plain English into a small, strictly
              validated <em>intent envelope</em> — a fixed, closed set of
              actions. Nothing it says is ever trusted or executed directly.
            </li>
            <li>
              <strong>Deterministic money logic.</strong> Every transaction is
              constructed by ordinary code with hard validation: known intents,
              re-verified signing, mainnet-only addresses, computed dust and
              relay limits. The LLM never touches money logic.
            </li>
            <li>
              <strong>Hardware-wallet signing.</strong> To spend, you sign the
              unsigned transaction on your own hardware wallet — the app never
              asks for or stores a private key.
            </li>
          </ul>
        </section>

        <section aria-labelledby="privacy">
          <h2 id="privacy">Privacy, honestly</h2>
          <p className={styles["privacy-note"]}>
            By default the app queries the public Esplora indexer to watch your
            addresses. That means the addresses you look up can be associated
            with your IP. If that matters to you, self-host an Esplora instance
            and point the app at it — nothing else changes.
          </p>
        </section>

        <section aria-labelledby="install">
          <h2 id="install">Install</h2>
          <p className={styles.hint}>
            Runs on macOS and Linux. Installs uv and Python&nbsp;3.12 if needed,
            then the wallet itself.
          </p>
          <div className={styles["install-block"]}>
            <code aria-label="Install command">
              curl -fsSL https://unsaltedbutter.ai/install | bash
            </code>
            <CopyButton />
          </div>
        </section>

        <p className={styles.repo}>
          Source:{" "}
          <a href="https://github.com/unsaltedbutter-ai/local-wallet">
            github.com/unsaltedbutter-ai/local-wallet
          </a>
        </p>
      </main>

      <footer className={styles.foot}>
        <p>Watch-only. Local-first. Keys never leave your hardware.</p>
      </footer>
    </div>
  );
}
