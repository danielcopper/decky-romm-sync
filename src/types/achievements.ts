/**
 * Achievements types — RetroAchievements integration: per-achievement metadata,
 * per-user earned records, and the summary/list/progress envelope shapes
 * returned by the backend.
 */

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
  message?: string;
}
