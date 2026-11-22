---
name: Sabah Brifingi
description: Günlük hava durumu, takvim, önemli okunmamış e-postalar ve aktif takiplerden kısa, enerjik sabah özeti üretir.
---

# Sabah Brifingi Yeteneği

Kullanıcı sabah brifingi istediğinde veya sabah 08:30 zamanlayıcısı tetiklendiğinde şu sırayı izle:

1. `USER.md` içinde kayıtlı şehri kontrol et (yoksa `Istanbul` kullan) ve `get_weather` aracını çağır.
2. Google Workspace bağlıysa:
   - `calendar_list_events(days_ahead=1)` ile günün toplantılarını al.
   - `gmail_list_messages(query="is:unread", max_results=5)` ile acil/önemli e-postaları tara.
   *(Bağlı değilse hata mesajı verme, o bölümü sessizce atla.)*
3. `list_scheduled_jobs` ve `list_web_watchers` ile aktif takip ve hatırlatmaları kontrol et.
4. Çıktıyı şu temiz yapıda Türkçe sun:
   - **Hava Durumu** (1 satır)
   - **Günün Takvimi** (saat sırasıyla)
   - **Önemli E-postalar** (varsa en kritik 3 madde)
   - **Günün Odağı** (kısa motivasyon / öncelik notu)
