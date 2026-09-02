"use client";

import { Suspense } from "react";
import { useRouter } from "next/navigation";
import { BankSelector } from "@/components/bank-selector";
import { Sidebar } from "@/components/sidebar";
import { StmView } from "@/components/stm-view";
import { useBank } from "@/lib/bank-context";
import { bankRoute } from "@/lib/bank-url";

export default function StmPage() {
  const router = useRouter();
  const { currentBank } = useBank();

  const handleTabChange = (
    tab:
      | "home"
      | "recall"
      | "reflect"
      | "data"
      | "documents"
      | "entities"
      | "knowledge"
      | "profile"
  ) => {
    if (!currentBank) return;
    router.push(bankRoute(currentBank, `?view=${tab}`));
  };

  return (
    <div className="h-screen overflow-hidden bg-background flex flex-col">
      <div className="shrink-0">
        <BankSelector />
      </div>
      <div className="flex flex-1 min-h-0 overflow-hidden">
        <Sidebar currentTab="home" onTabChange={handleTabChange} />
        <main className="flex-1 min-w-0 overflow-y-auto bg-[#111]">
          <Suspense
            fallback={
              <div className="stm-page" style={{ padding: 16, color: "#ddd" }}>
                STM
              </div>
            }
          >
            <StmView />
          </Suspense>
        </main>
      </div>
    </div>
  );
}
