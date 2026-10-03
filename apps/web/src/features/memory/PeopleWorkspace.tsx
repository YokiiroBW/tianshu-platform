import { lazy, Suspense } from "react";

const PeoplePage = lazy(() => import("../people/PeoplePage"));
const MemoryPage = lazy(() => import("./MemoryPage"));

export default function PeopleWorkspace({ section }: { section: number }) {
  return (
    <Suspense fallback={<p role="status">正在打开用户空间…</p>}>
      {section === 0 ? (
        <PeoplePage section={section} />
      ) : (
        <MemoryPage section={section} groupsOnly={section === 1} />
      )}
    </Suspense>
  );
}
