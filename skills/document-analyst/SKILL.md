---
name: Belge ve Sunum Analisti
description: PDF, PowerPoint (.pptx), Word (.docx), Excel (.xlsx) ve görselleri yapılandırılmış yönetici özetine dönüştürür.
---

# Belge ve Sunum Analisti Yeteneği

Kullanıcı bir belge (PDF, PPTX, DOCX, XLSX) veya görsel gönderdiğinde:

1. Belge zaten metin olarak geldiyse doğrudan analiz et; eski bir dosya soruluyorsa `list_uploaded_files` ve `analyze_uploaded_file` araçlarını kullan.
2. Analizi daima şu düzenli yapıyla sun:
   - **Kısa Özet (3 cümle)**
   - **Ana Bulgular / Kritik Noktalar** (madde madde)
   - **Aksiyon / Öneri** (varsa tarih, tutar, dikkat edilecek riskler)
3. Belgede kullanıcı için uzun vadede önemli tarih, proje veya karar varsa `remember_fact` ile hafızaya al.
