import { LoginLink, useAuth } from "../../app/Auth";
import PersonaAuthor from "./PersonaAuthor";
import "./personas.css";

export default function PersonaPanel() {
  const { session, loading, error, refresh } = useAuth();

  return (
    <section className="persona-page glass" aria-label="人格管理">
      <header className="persona-page-heading">
        <h2>人格</h2>
      </header>
      {loading ? (
        <p role="status">正在读取人格…</p>
      ) : error ? (
        <div role="alert">
          <p>{error}</p>
          <button className="button" onClick={() => void refresh()}>
            重新连接
          </button>
        </div>
      ) : session?.authenticated ? (
        <PersonaAuthor session={session} />
      ) : (
        <LoginLink />
      )}
    </section>
  );
}
