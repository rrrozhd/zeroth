"use client";

import { createContext, useContext } from "react";

type AuditVerificationState = {
  verifiedAt: string | null;
  markVerified: (at: string) => void;
};

export const AuditVerificationCtx = createContext<AuditVerificationState>({
  verifiedAt: null,
  markVerified: () => {},
});

export const useAuditVerification = () => useContext(AuditVerificationCtx);
