---
name: Kendi Kendini Geliştirme ve Hafıza
description: Hermes yaklaşımıyla kullanıcıyı her sohbette daha iyi tanır ve tekrarlayan iş akışlarını kalıcı yeteneğe dönüştürür.
---

# Kendi Kendini Geliştirme (Hermes Döngüsü)

1. Kullanıcı kendisi hakkında kalıcı bir bilgi paylaştığında (mesleği, çalışma saatleri, sevdiği üslup, ailesi/ekibi, sık çalıştığı projeler) bunu anında `update_user_profile` ile `USER.md` dosyasına ekle.
2. Önemli proje kararlarını, şifresiz sabit bilgileri ve uzun vadeli hedefleri `remember_fact(also_add_to_memory_md=True)` ile kaydet.
3. Kullanıcı 2 veya daha fazla kez benzer çok adımlı bir iş akışı tarif ederse, işi tamamladıktan sonra `create_skill` ile o iş akışını kalıcı bir `SKILL.md` olarak kaydet ve kullanıcıya yeni yetenek kazandığını haber ver.
