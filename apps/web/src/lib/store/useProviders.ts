"use client";

import { useCallback, useState } from "react";
import * as api from "../runtime";
import type { Providers } from "../types";

export function useProviders() {
  const [providers, setProviders] = useState<Providers>({});
  const [defaultProvider, setDefaultProvider] = useState("custom");

  const reloadProviders = useCallback(async () => {
    try {
      const r = await api.getProviders();
      setProviders(r.providers);
      setDefaultProvider(r.default_provider);
    } catch {
      /* ignore */
    }
  }, []);

  return { providers, defaultProvider, reloadProviders };
}
