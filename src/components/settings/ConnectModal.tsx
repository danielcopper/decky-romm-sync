/**
 * One-time credential prompt for minting a RomM Client API Token.
 *
 * The username + password entered here are write-only: never pre-filled,
 * never echoed back by the backend. On submit the parent calls
 * `connect_with_credentials`, which exchanges them for a scoped token and
 * discards the password. This modal owns only the in-flight field values
 * and hands both to `onConnect` — token minting, status, and persistence
 * live in the parent (SettingsPage).
 */

import { FC, useState, ChangeEvent } from "react";
import { ConfirmModal, TextField } from "@decky/ui";

interface ConnectModalProps {
  closeModal?: () => void;
  onConnect: (username: string, password: string) => void;
}

export const ConnectModal: FC<ConnectModalProps> = ({ closeModal, onConnect }) => {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  return (
    <ConfirmModal
      {...(closeModal === undefined ? {} : { closeModal })}
      onOK={() => {
        onConnect(username, password);
      }}
      strTitle="Connect to RomM"
      strOKButtonText="Connect"
      bDisableBackgroundDismiss={true}
    >
      <div style={{ fontSize: "12px", marginBottom: "12px", color: "rgba(255,255,255,0.6)" }}>
        Enter your RomM username and password once. The plugin exchanges them for an API token and never stores your
        password.
      </div>
      <TextField
        focusOnMount={true}
        label="Username"
        value={username}
        onChange={(e: ChangeEvent<HTMLInputElement>) => setUsername(e.target.value)}
      />
      <TextField
        label="Password"
        value={password}
        bIsPassword
        onChange={(e: ChangeEvent<HTMLInputElement>) => setPassword(e.target.value)}
      />
    </ConfirmModal>
  );
};
