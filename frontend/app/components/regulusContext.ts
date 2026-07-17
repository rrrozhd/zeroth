"use client";

import { createContext, useContext } from "react";
import type { RegulusStatus } from "@/app/lib/regulus";

export const RegulusCtx = createContext<RegulusStatus>("unknown");
export const useRegulus = () => useContext(RegulusCtx);
