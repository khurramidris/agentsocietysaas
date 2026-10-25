import { useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import type { Meta } from "@/api/types";

declare global {
  interface Window {
    process?: { env: Record<string, string> };
  }
}

/** Public deployment metadata (brand, features, defaults). */
export function useMeta() {
  return useQuery({
    queryKey: ["meta"],
    queryFn: async () => {
      const meta = await api.get<Meta>("/api/v1/meta");
      // The upstream replay map reads process.env.MAPBOX_ACCESS_TOKEN when its module
      // loads; the replay page is loaded lazily, after this runs.
      window.process = { env: { ...(window.process?.env ?? {}), MAPBOX_ACCESS_TOKEN: meta.mapbox_token ?? "" } };
      document.title = meta.brand_name;
      return meta;
    },
    staleTime: Infinity,
  });
}
