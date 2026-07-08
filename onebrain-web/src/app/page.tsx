import { getSession, onebrainApiBaseUrl, type SessionInfo } from "@/lib/onebrain-api";

const navItems = ["Chat", "Documents", "Spaces", "Privacy", "Operator"];

export default async function Home() {
  let session: SessionInfo | null = null;
  let apiState = "Connected";
  try {
    session = await getSession();
  } catch {
    apiState = "Unavailable";
  }
  const apiBaseUrl = onebrainApiBaseUrl();

  return (
    <main className="shell">
      <aside className="sidebar" aria-label="Main navigation">
        <div className="brand">
          <span className="brandMark">one</span>
          <span>brain</span>
        </div>
        <nav className="nav">
          {navItems.map((item, index) => (
            <a className={index === 0 ? "navItem active" : "navItem"} href="#" key={item}>
              {item}
            </a>
          ))}
        </nav>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <p className="eyebrow">Web console</p>
            <h1>OneBrain</h1>
          </div>
          <div className={session ? "status signedIn" : "status signedOut"}>
            <span className="statusDot" />
            {session ? "Session active" : apiState}
          </div>
        </header>

        <div className="grid">
          <section className="panel primaryPanel">
            <div>
              <p className="panelLabel">Current user</p>
              <h2>{session?.display_name || session?.email || "No active session"}</h2>
            </div>
            <dl className="facts">
              <div>
                <dt>Role</dt>
                <dd>{session?.role_label || "None"}</dd>
              </div>
              <div>
                <dt>Tenant</dt>
                <dd>{session?.tenant_id || "None"}</dd>
              </div>
              <div>
                <dt>Clearance</dt>
                <dd>{session?.clearance || "None"}</dd>
              </div>
            </dl>
          </section>

          <section className="panel">
            <p className="panelLabel">Core API</p>
            <h2>{apiBaseUrl.replace(/^https?:\/\//, "")}</h2>
            <div className="metricRow">
              <span>API state</span>
              <strong>{apiState}</strong>
            </div>
          </section>

          <section className="panel listPanel">
            <p className="panelLabel">Migration queue</p>
            <ul>
              <li>Typed API boundary</li>
              <li>Chat surface</li>
              <li>Document library</li>
              <li>Privacy center</li>
            </ul>
          </section>
        </div>
      </section>
    </main>
  );
}
