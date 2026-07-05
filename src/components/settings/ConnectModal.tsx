/**
 * One-time prompt for signing in to RomM — either by minting a Client API
 * Token from a username + password, or by pasting a token created in RomM's
 * web UI (the path for OIDC accounts, which have no password to mint from).
 *
 * Both the credentials and the pasted token are write-only: never pre-filled,
 * never echoed back by the backend. On submit the parent's matching handler
 * runs — `connect_with_credentials` (which exchanges the credentials for a
 * scoped token and discards the password) or `connect_with_token` (which
 * validates and stores the pasted token). This modal owns only the in-flight
 * field values and the selected mode; token minting/validation, status, and
 * persistence live in the parent (SettingsPage).
 */

import { FC, useState, ChangeEvent } from "react";
import { ConfirmModal, TextField, DropdownItem } from "@decky/ui";

type SignInMode = "credentials" | "token";

interface ConnectModalProps {
  closeModal?: () => void;
  onConnect: (username: string, password: string) => void;
  onConnectToken: (token: string) => void;
}

export const ConnectModal: FC<ConnectModalProps> = ({ closeModal, onConnect, onConnectToken }) => {
  const [mode, setMode] = useState<SignInMode>("credentials");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");

  return (
    <ConfirmModal
      {...(closeModal === undefined ? {} : { closeModal })}
      onOK={() => {
        if (mode === "token") {
          onConnectToken(token);
        } else {
          onConnect(username, password);
        }
      }}
      strTitle="Sign in to RomM"
      strOKButtonText="Sign in"
      bDisableBackgroundDismiss={true}
    >
      <DropdownItem
        label="Sign-in method"
        rgOptions={[
          { data: "credentials", label: "Username & password" },
          { data: "token", label: "API token" },
        ]}
        selectedOption={mode}
        onChange={(option) => setMode(option.data as SignInMode)}
      />
      {mode === "token" ? (
        <>
          <div style={{ fontSize: "12px", marginBottom: "12px", color: "rgba(255,255,255,0.6)" }}>
            Create a token in RomM&apos;s web UI (Settings → API Tokens) and paste it here. Make sure it has the scopes
            listed in the plugin docs so downloads, saves, and device sync work. The plugin never deletes a pasted
            token; you manage it in RomM.
          </div>
          <TextField
            focusOnMount={true}
            label="API Token"
            value={token}
            bIsPassword
            onChange={(e: ChangeEvent<HTMLInputElement>) => setToken(e.target.value)}
          />
        </>
      ) : (
        <>
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
        </>
      )}
    </ConfirmModal>
  );
};
