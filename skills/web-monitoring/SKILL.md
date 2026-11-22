---
name: Web ve Fiyat Takipçisi
description: Ürün fiyatları, haberler, duyurular veya rakip siteleri 7/24 izlemek için takipçi kurar ve araştırma yapar.
---

# Web ve Fiyat Takipçisi Yeteneği

Kullanıcı bir ürün fiyatını, site duyurusunu veya konuyu takip etmeni istediğinde:

1. Önce URL verilmemişse `web_search` ile doğru kaynağı bul ve `fetch_webpage` ile doğrula.
2. Tek seferlik soruysa net özeti, kaynak linkiyle birlikte ver.
3. Sürekli takip isteniyorsa `add_web_watcher` aracını kullanarak takipçi oluştur:
   - `interval_minutes`: Fiyat/haber için 60–180 dk, acil duyurular için 15–30 dk seç.
   - `instruction`: Hangi koşulda bildirim atılacağını net yaz (örn. "Fiyat 20.000 TL altına düşerse veya stok açılırsa bildir").
4. Takipçi ID'sini ve kontrol aralığını kullanıcıya bildir.
