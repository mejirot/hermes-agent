/** Copy owned by the Bot-specific saved cron history view. */
export interface CronHistoryMessages {
  historyEmpty: string
  historyFailedLoad: string
  historyReadFailure: string
  historyOpenFailed: string
  historyUntitled: string
}

export const CRON_HISTORY_LOCALES = {
  en: {
    historyEmpty: 'No cron run history for this bot yet.',
    historyFailedLoad: 'Could not load run history',
    historyReadFailure: 'Could not refresh run history. Try again.',
    historyOpenFailed: 'Could not open this run',
    historyUntitled: 'Untitled cron run'
  },
  ja: {
    historyEmpty: 'このボットの定期実行履歴はまだありません。',
    historyFailedLoad: '実行履歴を読み込めませんでした',
    historyReadFailure: '実行履歴を更新できませんでした。もう一度お試しください。',
    historyOpenFailed: 'この実行履歴を開けませんでした',
    historyUntitled: '無題の定期実行'
  },
  zh: {
    historyEmpty: '此机器人暂无定时任务执行历史。',
    historyFailedLoad: '无法加载执行历史',
    historyReadFailure: '无法刷新执行历史。请重试。',
    historyOpenFailed: '无法打开此次执行',
    historyUntitled: '未命名的定时执行'
  },
  'zh-hant': {
    historyEmpty: '此機器人尚無定期任務執行歷史。',
    historyFailedLoad: '無法載入執行歷史',
    historyReadFailure: '無法重新整理執行歷史。請重試。',
    historyOpenFailed: '無法開啟此次執行',
    historyUntitled: '未命名的定期執行'
  },
  ar: {
    historyEmpty: 'لا يوجد سجل تشغيل مجدول لهذا البوت بعد.',
    historyFailedLoad: 'تعذّر تحميل سجل التشغيل',
    historyReadFailure: 'تعذّر تحديث سجل التشغيل. حاول مرة أخرى.',
    historyOpenFailed: 'تعذّر فتح عملية التشغيل هذه',
    historyUntitled: 'تشغيل مجدول بلا عنوان'
  },
  ru: {
    historyEmpty: 'У этого бота пока нет истории запусков по расписанию.',
    historyFailedLoad: 'Не удалось загрузить историю запусков',
    historyReadFailure: 'Не удалось обновить историю запусков. Попробуйте ещё раз.',
    historyOpenFailed: 'Не удалось открыть этот запуск',
    historyUntitled: 'Запуск по расписанию без названия'
  },
  fr: {
    historyEmpty: 'Ce bot n’a pas encore d’historique d’exécution planifiée.',
    historyFailedLoad: 'Impossible de charger l’historique des exécutions',
    historyReadFailure: 'Impossible d’actualiser l’historique. Réessayez.',
    historyOpenFailed: 'Impossible d’ouvrir cette exécution',
    historyUntitled: 'Exécution planifiée sans titre'
  },
  de: {
    historyEmpty: 'Für diesen Bot gibt es noch keinen Verlauf geplanter Ausführungen.',
    historyFailedLoad: 'Ausführungsverlauf konnte nicht geladen werden',
    historyReadFailure: 'Der Ausführungsverlauf konnte nicht aktualisiert werden. Bitte erneut versuchen.',
    historyOpenFailed: 'Diese Ausführung konnte nicht geöffnet werden',
    historyUntitled: 'Unbenannte geplante Ausführung'
  },
  es: {
    historyEmpty: 'Este bot aún no tiene historial de ejecuciones programadas.',
    historyFailedLoad: 'No se pudo cargar el historial de ejecuciones',
    historyReadFailure: 'No se pudo actualizar el historial. Inténtalo de nuevo.',
    historyOpenFailed: 'No se pudo abrir esta ejecución',
    historyUntitled: 'Ejecución programada sin título'
  }
} satisfies Record<string, CronHistoryMessages>
