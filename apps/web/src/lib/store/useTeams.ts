"use client";

import { useCallback, useState } from "react";
import * as api from "../runtime";
import type { Team } from "../types";

export function useTeams() {
  const [teams, setTeams] = useState<Team[]>([]);
  const notifyTeamUpdated = useCallback((team: Team) => {
    if (!team?.id) return;
    setTeams((prev) => {
      const i = prev.findIndex((t) => t.id === team.id);
      return i >= 0 ? prev.map((t, k) => (k === i ? team : t)) : [...prev, team];
    });
  }, []);
  const reloadTeams = useCallback(async () => {
    try {
      const r = await api.listTeams();
      setTeams(r.teams ?? []);
    } catch {
      /* sidecar down */
    }
  }, []);

  return { teams, reloadTeams, notifyTeamUpdated };
}
