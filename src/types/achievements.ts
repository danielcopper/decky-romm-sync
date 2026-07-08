/**
 * Achievements types — RetroAchievements integration: per-achievement metadata,
 * per-user earned records, and the summary/list/progress envelope shapes
 * returned by the backend.
 */

import type { RommErrorCode } from "./api";

export interface Achievement {
  ra_id: number;
  badge_id: string;
  title: string;
  description: string;
  points: number;
  badge_url: string;
  badge_url_lock: string;
  display_order: number;
  type: string;
  num_awarded: number;
  num_awarded_hardcore: number;
}

export interface EarnedAchievement {
  id: string;
  date: string;
  date_hardcore: string | null;
}

export interface AchievementSummary {
  earned: number;
  total: number;
  earned_hardcore: number;
  cached_at?: number;
}

export interface AchievementList {
  success: boolean;
  achievements: Achievement[];
  total: number;
  no_ra_id?: boolean;
  stale?: boolean;
  // Present on a failure ({success: false}); "server_unreachable" is the only
  // reason the offline feed acts on (#1345).
  reason?: RommErrorCode;
  message?: string;
}

export interface AchievementProgress {
  success: boolean;
  earned: number;
  earned_hardcore?: number;
  total: number;
  earned_achievements: EarnedAchievement[];
  no_ra_id?: boolean;
  stale?: boolean;
  // Present on a failure ({success: false}); "server_unreachable" is the only
  // reason the offline feed acts on. "no_ra_username" is a config gap, not a
  // connectivity verdict, so the feed leaves the store untouched for it (#1345).
  reason?: RommErrorCode | "no_ra_username";
  message?: string;
}
