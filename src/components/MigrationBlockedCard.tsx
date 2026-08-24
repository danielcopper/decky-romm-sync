import { FC } from "react";
import { WarningCard } from "./WarningCard";

interface MigrationBlockedCardProps {
  /** Compact mode for narrow contexts (QAM panel). */
  compact?: boolean;
}

/** Polished warning card shown on the game detail page when a RetroDECK migration is pending. */
export const MigrationBlockedCard: FC<MigrationBlockedCardProps> = ({ compact = false }) => (
  <WarningCard
    title="RetroDECK Migration Required"
    message="Open the plugin QAM to migrate files or dismiss the migration before playing."
    compact={compact}
  />
);
