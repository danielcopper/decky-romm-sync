/**
 * Device registration types — the registered-device record returned by the
 * RomM server and the wrapper response for the bulk listing. Anything that
 * describes a save-sync device entry from the server lives here.
 */

export interface RegisteredDevice {
  id: string;
  name: string | null;
  platform: string | null;
  client: string | null;
  client_version: string | null;
  last_seen: string | null;
  created_at: string;
  is_current_device: boolean;
  user_id?: number;
  ip_address?: string | null;
  mac_address?: string | null;
  hostname?: string | null;
  sync_mode?: string | null;
  sync_enabled?: boolean;
  updated_at?: string | null;
}

export interface ListDevicesResponse {
  success: boolean;
  devices: RegisteredDevice[];
  disabled?: boolean;
  reason?: string;
  message?: string;
}
