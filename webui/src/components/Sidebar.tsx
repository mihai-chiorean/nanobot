import {
  BriefcaseBusiness,
  Moon,
  PanelLeftClose,
  RefreshCcw,
  Settings,
  SquarePen,
  Sun,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { ChatList } from "@/components/ChatList";
import { ConnectionBadge } from "@/components/ConnectionBadge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import type { ChatSummary } from "@/lib/types";

interface SidebarProps {
  sessions: ChatSummary[];
  activeKey: string | null;
  loading: boolean;
  theme: "light" | "dark";
  onToggleTheme: () => void;
  onNewChat: () => void;
  onSelect: (key: string) => void;
  onRefresh: () => void;
  onRequestDelete: (key: string, label: string) => void;
  onCollapse: () => void;
  activeView?: "chat" | "activity" | "settings";
  guestMode?: boolean;
  onOpenActivity: () => void;
  onOpenSettings: () => void;
}

export function Sidebar(props: SidebarProps) {
  const { t } = useTranslation();
  return (
    <aside className="flex h-full w-full flex-col border-r border-sidebar-border/70 bg-sidebar text-sidebar-foreground">
      <div className="flex items-center justify-between px-3 pb-2 pt-3">
        <div className="flex min-w-0 items-center gap-2">
          <img
            src="/brand/ziggy_icon.png"
            alt=""
            className="h-7 w-7 select-none rounded-lg object-contain"
            aria-hidden
            draggable={false}
          />
          <div className="min-w-0 leading-none">
            <div className="truncate text-[15px] font-semibold tracking-normal">Ziggy</div>
            <div className="truncate text-[10.5px] font-medium text-muted-foreground">
              private agent workspace
            </div>
          </div>
        </div>
        <div className="flex items-center gap-0.5">
          <Button
            variant="ghost"
            size="icon"
            aria-label={t("sidebar.toggleTheme")}
            onClick={props.onToggleTheme}
            className="h-7 w-7 rounded-lg text-muted-foreground hover:bg-sidebar-accent hover:text-sidebar-foreground"
          >
            {props.theme === "dark" ? (
              <Sun className="h-3.5 w-3.5" />
            ) : (
              <Moon className="h-3.5 w-3.5" />
            )}
          </Button>
          <Button
            variant="ghost"
            size="icon"
            aria-label={t("sidebar.collapse")}
            onClick={props.onCollapse}
            className="h-7 w-7 rounded-lg text-muted-foreground hover:bg-sidebar-accent hover:text-sidebar-foreground"
          >
            <PanelLeftClose className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>
      <div className="px-2 pb-2">
        <Button
          onClick={props.onNewChat}
          className="h-9 w-full justify-start gap-2 rounded-full px-3 text-[13px] font-medium text-sidebar-foreground/90 hover:bg-sidebar-accent hover:text-sidebar-foreground"
          variant="ghost"
        >
          <SquarePen className="h-3.5 w-3.5" />
          {t("sidebar.newChat")}
        </Button>
      </div>
      <div className="flex items-center justify-between px-3 pb-1.5 pt-2.5 text-[11px] font-medium text-muted-foreground">
        <span>{t("sidebar.recent")}</span>
        <Button
          variant="ghost"
          size="icon"
          className="h-6 w-6 rounded-md text-muted-foreground hover:bg-sidebar-accent hover:text-sidebar-foreground"
          onClick={props.onRefresh}
          aria-label={t("sidebar.refreshSessions")}
        >
          <RefreshCcw className="h-3.5 w-3.5" />
        </Button>
      </div>
      <div className="flex-1 overflow-hidden">
        <ChatList
          sessions={props.sessions}
          activeKey={props.activeKey}
          loading={props.loading}
          onSelect={props.onSelect}
          onRequestDelete={props.onRequestDelete}
        />
      </div>
      <Separator className="bg-sidebar-border/50" />
      <div className="flex items-center justify-between gap-2 px-2.5 py-2 text-xs">
        <ConnectionBadge />
        {props.guestMode ? null : (
          <div className="flex items-center gap-1">
            <Button
              onClick={props.onOpenActivity}
              className="h-7 gap-1.5 rounded-md px-2 text-[11px] text-muted-foreground hover:bg-sidebar-accent hover:text-sidebar-foreground"
              variant={props.activeView === "activity" ? "secondary" : "ghost"}
            >
              <BriefcaseBusiness className="h-3.5 w-3.5" />
              Work
            </Button>
            <Button
              onClick={props.onOpenSettings}
              className="h-7 gap-1.5 rounded-md px-2 text-[11px] text-muted-foreground hover:bg-sidebar-accent hover:text-sidebar-foreground"
              variant={props.activeView === "settings" ? "secondary" : "ghost"}
            >
              <Settings className="h-3.5 w-3.5" />
              Settings
            </Button>
          </div>
        )}
      </div>
    </aside>
  );
}
