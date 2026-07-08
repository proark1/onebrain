export function SignedOutState({ apiBaseUrl }: { apiBaseUrl: string }) {
  return (
    <main className="stateScreen">
      <section className="statePanel">
        <div className="brand">
          <span className="brandMark">one</span>
          <span>brain</span>
        </div>
        <h1>Sign in to continue</h1>
        <p>Use the existing OneBrain login, then return here to work in the new web console.</p>
        <a className="stateAction" href={apiBaseUrl}>Open login</a>
      </section>
    </main>
  );
}

export function ApiUnavailableState({ apiBaseUrl }: { apiBaseUrl: string }) {
  return (
    <main className="stateScreen">
      <section className="statePanel">
        <div className="brand">
          <span className="brandMark">one</span>
          <span>brain</span>
        </div>
        <h1>Core API unavailable</h1>
        <p>Start the FastAPI service and refresh this page. The web console is configured for {apiBaseUrl}.</p>
      </section>
    </main>
  );
}
