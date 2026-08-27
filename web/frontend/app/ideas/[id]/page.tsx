import { Suspense } from "react";

import { IdeaDetailClient } from "@/components/idea-detail/idea-detail-client";
import { Skeleton } from "@/components/ui/skeleton";

export default function IdeaDetailPage() {
  return (
    <Suspense
      fallback={
        <div className="space-y-4 p-6">
          <Skeleton className="h-8 w-1/3" />
          <Skeleton className="h-40 w-full" />
          <Skeleton className="h-64 w-full" />
        </div>
      }
    >
      <IdeaDetailClient />
    </Suspense>
  );
}
