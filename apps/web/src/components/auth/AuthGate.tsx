"use client";

import { Loader2, LockKeyhole, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, setToken } from "@/lib/api";

type State = "checking" | "ready" | "login" | "offline";

/** 启动时探测认证：本机回环后端会直接放行；远程访问显示访问密钥输入框。 */
export function AuthGate({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<State>("checking");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const probe = useCallback(async () => {
    setState("checking");
    setError("");
    try {
      await api.me();
      setState("ready");
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setState(msg.includes("401") || msg.includes("未认证") ? "login" : "offline");
    }
  }, []);

  useEffect(() => {
    probe();
    const onRequired = () => setState("login");
    window.addEventListener("auth:required", onRequired);
    return () => window.removeEventListener("auth:required", onRequired);
  }, [probe]);

  const submit = async () => {
    if (!key.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      const res = await api.login(key.trim());
      setToken(res.token);
      window.location.reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "登录失败");
      setBusy(false);
    }
  };

  if (state === "ready") return <>{children}</>;

  if (state === "checking") {
    return (
      <div className="flex h-screen items-center justify-center text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" /> 正在连接后端…
      </div>
    );
  }

  return (
    <div className="flex h-screen items-center justify-center bg-secondary/30 px-4">
      <div className="w-full max-w-sm rounded-2xl border bg-card p-6 shadow-sm">
        <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-primary/10 text-primary">
          <LockKeyhole className="h-5 w-5" />
        </div>
        <h1 className="mt-4 text-lg font-semibold">访问受限</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {state === "offline"
            ? "无法连接后端服务，请确认 API 已启动（默认 8900 端口）。"
            : "请输入服务端配置的 API Key（API_KEYS 中的任意一个）。"}
        </p>

        {state === "offline" ? (
          <Button className="mt-4 w-full gap-2" onClick={probe}>
            <RefreshCw className="h-4 w-4" /> 重试连接
          </Button>
        ) : (
          <>
            <Input
              className="mt-4"
              type="password"
              placeholder="访问密钥"
              value={key}
              autoFocus
              onChange={(e) => setKey(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") submit();
              }}
            />
            {error && <div className="mt-2 text-xs text-rose-600">{error}</div>}
            <Button className="mt-4 w-full" disabled={busy} onClick={submit}>
              {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
              进入
            </Button>
          </>
        )}
      </div>
    </div>
  );
}
