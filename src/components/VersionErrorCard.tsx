import { FC, createElement, useEffect, useState } from "react";
import { getVersionError, onVersionErrorChange } from "../utils/connectionState";
import { WarningCard } from "./WarningCard";

/**
 * Subscribe to version error state changes.
 * Returns the current version error (or null).
 */
export function useVersionError(): string | null {
  const [err, setErr] = useState<string | null>(getVersionError());
  useEffect(() => onVersionErrorChange(setErr), []);
  return err;
}

interface VersionErrorCardProps {
  message: string;
  /** Compact mode for narrow contexts (QAM panel). */
  compact?: boolean;
}

/** Polished error card shown when server version is below plugin minimum. */
export const VersionErrorCard: FC<VersionErrorCardProps> = ({ message, compact = false }) =>
  createElement(WarningCard, { title: "RomM Server Update Required", message, compact });
