export function SignedOutState({ loginHref }: { loginHref: string }) {
  return (
    <main className="stateScreen">
      <section className="statePanel">
        <div className="brand">
          <span className="brandMark">AD</span>
          <span>OneBrain</span>
        </div>
        <h1>Sign in to continue</h1>
        <p>Sign in with your OneBrain account to continue working in the web console.</p>
        <a className="stateAction" href={loginHref}>Open login</a>
      </section>
    </main>
  );
}

export function ApiUnavailableState({ apiBaseUrl }: { apiBaseUrl: string }) {
  return (
    <main className="stateScreen">
      <section className="statePanel">
        <div className="brand">
          <span className="brandMark">AD</span>
          <span>OneBrain</span>
        </div>
        <h1>Core API unavailable</h1>
        <p>Start the FastAPI service and refresh this page. The web console is configured for {apiBaseUrl}.</p>
      </section>
    </main>
  );
}
