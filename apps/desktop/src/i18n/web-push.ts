import { useI18n } from './context'

const en = {
  title: 'Phone and background notifications',
  description: 'Receive cron results, replies in Hermes, approvals and questions even with the app closed. This device only.',
  enable: 'Enable on this device', disable: 'Disable on this device', retry: 'Check again', test: 'Send push test',
  preview: 'Show a short preview', previewDescription: 'Include up to 160 characters on the lock screen. Turn off to hide message text.',
  kinds: { cron: 'Scheduled job results', reply: 'Bot and Group replies in Hermes', approval: 'Approval needed', input: 'Question needs an answer' },
  hint: 'A conversation you are viewing is quiet only after its new content has appeared. Ordinary Discord-only chat is excluded.',
  browserHint: 'On Android, allow notifications for Brave in both browser and Android settings. If push registration fails, try Chrome. You can install Hermes from the browser menu.',
  states: {
    loading: 'Checking this device…', ready: 'Push is ready. Enable it to receive notifications on this device.',
    subscribed: 'Enabled on this device.', unsupported: 'This browser does not provide the required push APIs. Try Android Brave or Chrome.',
    insecure: 'A secure HTTPS connection is required.', denied: 'Notifications are blocked. Allow them in this browser’s site settings and in Android settings, then check again.',
    unavailable: 'Push delivery is not enabled on this Hermes server.', error: 'Could not register or update push. Check connectivity and the browser notification settings, then retry.',
    'signed-out': 'Sign in to Hermes again to manage notifications.'
  },
  testSent: 'Push test queued. Check this device’s notifications.', testFailed: 'The push test could not be sent.',
  disableFailed: 'Could not disable delivery. Reconnect and retry.', opening: 'Opening the notification…',
  deliveryFailed: 'The last delivery failed. Check connectivity or disable and enable notifications again.',
  openFailed: 'This notification could not be opened. It may have expired, or its conversation may no longer be available.',
  signIn: 'Sign in', details: 'Notification details', profile: 'Profile', job: 'Scheduled job', execution: 'Execution',
  readOnly: 'This result has no saved conversation. These are the notification details; opening them does not run the job.',
  settings: 'Notification settings'
}

const ja: typeof en = {
  title: 'スマホ・バックグラウンド通知',
  description: 'アプリを閉じていても、cron の結果、Hermes 内の返信、承認・質問を受け取ります。この端末だけの設定です。',
  enable: 'この端末で有効にする', disable: 'この端末で無効にする', retry: '再確認', test: 'Push 通知をテスト',
  preview: '短い本文を表示', previewDescription: 'ロック画面に本文を最大160文字表示します。オフにすると本文を隠します。',
  kinds: { cron: 'cron の実行結果', reply: 'Hermes 内の Bot・Groups の返信', approval: '承認が必要', input: '質問への回答が必要' },
  hint: '表示中の会話は、新しい本文や要求が画面に表示されたと確認できた場合に通知を抑えます。Discord だけの通常会話は対象外です。',
  browserHint: 'Android の Brave と Android 本体の両方で通知を許可してください。購読できない場合は Chrome を試せます。ブラウザーのメニューから Hermes をインストールできます。',
  states: {
    loading: 'この端末を確認中…', ready: 'Push 通知を利用できます。この端末で有効にしてください。', subscribed: 'この端末で有効です。',
    unsupported: '必要な Push API がありません。Android の Brave または Chrome を試してください。',
    insecure: '安全な HTTPS 接続が必要です。', denied: '通知がブロックされています。ブラウザーのサイト設定と Android の設定で許可し、再確認してください。',
    unavailable: 'この Hermes サーバーでは Push 配信が有効になっていません。', error: '通知の登録・更新に失敗しました。接続とブラウザーの通知設定を確認し、再試行してください。',
    'signed-out': '通知を管理するには Hermes に再ログインしてください。'
  },
  testSent: 'テスト通知を配信待ちにしました。この端末の通知を確認してください。', testFailed: 'テスト通知を送信できませんでした。',
  disableFailed: '配信を無効にできませんでした。接続後に再試行してください。', opening: '通知を開いています…',
  deliveryFailed: '前回の配信に失敗しました。接続を確認するか、この端末の通知を一度無効にして登録し直してください。',
  openFailed: '通知を開けませんでした。有効期限が切れたか、対象の会話が利用できない可能性があります。',
  signIn: 'ログイン', details: '通知の詳細', profile: 'プロファイル', job: 'cron ジョブ', execution: '実行',
  readOnly: 'この結果には保存された会話がありません。通知の詳細を表示しています。開くだけでジョブが実行されることはありません。',
  settings: '通知設定'
}

export function useWebPushCopy(): typeof en {
  const { locale } = useI18n()
  return locale === 'ja' ? ja : en
}
