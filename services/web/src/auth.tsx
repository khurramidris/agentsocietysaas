import * as React from "react";

import { api, onSession, refreshSession, setSession, type Session } from "@/api/client";
import type { OrgSummary, User } from "@/api/types";

const ORG_KEY = "societas.org";

interface AuthValue {
  status: "loading" | "anonymous" | "authenticated";
  user: User | null;
  orgs: OrgSummary[];
  org: OrgSummary | null;
  setOrg: (orgId: string) => void;
  login: (email: string, password: string) => Promise<void>;
  signup: (body: { email: string; password: string; name?: string; org_name?: string }) => Promise<void>;
  logout: () => Promise<void>;
  reload: () => Promise<void>;
}

const AuthContext = React.createContext<AuthValue | null>(null);

function readStoredOrg(): string | null {
  try {
    return localStorage.getItem(ORG_KEY);
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = React.useState<AuthValue["status"]>("loading");
  const [user, setUser] = React.useState<User | null>(null);
  const [orgs, setOrgs] = React.useState<OrgSummary[]>([]);
  const [orgId, setOrgId] = React.useState<string | null>(readStoredOrg);

  React.useEffect(() => {
    const off = onSession((session: Session | null) => {
      setUser(session?.user ?? null);
      setOrgs(session?.orgs ?? []);
      setStatus(session ? "authenticated" : "anonymous");
    });
    void refreshSession();
    return () => {
      off();
    };
  }, []);

  const org = orgs.find((o) => o.id === orgId) ?? orgs.find((o) => o.is_personal) ?? orgs[0] ?? null;

  const value = React.useMemo<AuthValue>(
    () => ({
      status,
      user,
      orgs,
      org,
      setOrg: (id: string) => {
        setOrgId(id);
        try {
          localStorage.setItem(ORG_KEY, id);
        } catch {
          /* private mode */
        }
      },
      login: async (email, password) => {
        setSession(await api.post<Session>("/api/v1/auth/login", { email, password }));
      },
      signup: async (body) => {
        setSession(await api.post<Session>("/api/v1/auth/signup", body));
      },
      logout: async () => {
        try {
          await api.post("/api/v1/auth/logout");
        } finally {
          setSession(null);
        }
      },
      reload: async () => {
        const me = await api.get<{ user: User; orgs: OrgSummary[] }>("/api/v1/auth/me");
        setUser(me.user);
        setOrgs(me.orgs);
      },
    }),
    [status, user, orgs, org],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = React.useContext(AuthContext);
  if (!value) throw new Error("useAuth outside AuthProvider");
  return value;
}

const RANK = { viewer: 0, member: 1, admin: 2, owner: 3 } as const;

export function atLeast(role: string | undefined | null, needed: keyof typeof RANK): boolean {
  return role != null && role in RANK && RANK[role as keyof typeof RANK] >= RANK[needed];
}
