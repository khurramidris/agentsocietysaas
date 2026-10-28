import { useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import type { Meta } from "@/api/types";

/** Public deployment metadata (brand, features, defaults). */
export function useMeta() {
  return useQuery({
    queryKey: ["meta"],
    queryFn: async () => {
      const meta = await api.get<Meta>("/api/v1/meta");
      // The upstream replay map reads process.env.MAPBOX_ACCESS_TOKEN when its module
      // loads; the replay page is loaded lazily, after this runs.
      const w = window as unknown as { process?: { env: Record<string, string> } };
      w.process = { env: { ...(w.process?.env ?? {}), MAPBOX_ACCESS_TOKEN: meta.mapbox_token ?? "" } };
      document.title = meta.brand_name;
      return meta;
    },
    staleTime: Infinity,
  });
}
