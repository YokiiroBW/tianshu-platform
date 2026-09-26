// All feature requests keep their own cancellation and error handling. Only a
// possible session loss is sent to the shell for authoritative session revalidation.
export async function webFetch(input: string, init?: RequestInit) {
  const response = await fetch(input, init);
  if (
    !init?.signal?.aborted &&
    response.headers.get("content-type")?.includes("application/json")
  ) {
    const result = await response
      .clone()
      .json()
      .catch(() => null);
    if (
      !init?.signal?.aborted &&
      input !== "/api/web/login" &&
      ((!response.ok &&
        ["session_expired", "unauthorized"].includes(result?.code)) ||
        (response.ok &&
          input === "/api/web/session" &&
          result?.authenticated === false))
    )
      window.dispatchEvent(new Event("tianshu:session-lost"));
  }
  return response;
}
