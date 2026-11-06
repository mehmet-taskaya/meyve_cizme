# Proje 2: Kamerayla El Takibi ve Hareketle Fare Kontrolü

`opencv-python` ve `mediapipe` kullanılarak geliştirilmiş gerçek zamanlı el takibi, parmak sayma ve temassız fare/nesne kontrol uygulaması.

## Özellikler

### Temel Görevler
1. **Canlı Kamera Görüntüsü**: Webcam üzerinden ayna görüntüsü (mirror mode) ile gerçek zamanlı akış.
2. **El Tespiti ve Eklem Çizimi**: MediaPipe ile el üzerindeki 21 eklem noktası (landmarks) ve bağlantı çizgileri gerçek zamanlı çizilir; açık olan parmak uçları yeşil, kapalı olanlar kırmızı renkle vurgulanır.
3. **Açık Parmak Sayısı Hesaplama**: Hem sağ hem sol el için 0–5 arası açık parmak sayısı ve hangi parmakların açık olduğu anlık olarak ekranda gösterilir.

### Bonus Özellikler (Fare ve Nesne Yönetimi)
- **İmleç Yönetimi (Başparmak + İşaret Parmağı Birleştirme / Pinch)**: Başparmağınız ile işaret parmağınızı birleştirip tuttuğunuzda bilgisayarınızın fare imleci elinizin hareketini sarsıntısız (yumuşatılmış) şekilde takip eder.
- **Sol Tık ve Çift Tık (Serçe Parmak)**:
  - **Tek Sol Tık**: **Serçe parmağınızı** **1 kez** açıp kapattığınızda **Tek Sol Tık** atılır.
  - **Çift Sol Tık**: **Serçe parmağınızı** arka arkaya **2 kez** açıp kapattığınızda **Çift Sol Tık** atılır.
- **Sağ Tık (Orta Parmak)**: **Orta parmağınızı** kaldırıp indirdiğinizde **Sağ Tık** yapar.
- **Yumruk Yaparak Nesne Kaydırma (Fist / 0 Parmak Açık)**:
  - **Sanal Nesneler**: Kamera ekranındaki renkli kutuların (`Nesne 1`, `Nesne 2`, `Nesne 3`) üzerinde elinizi yumruk yaparak kutuları tutup istediğiniz yere kaydırabilirsiniz.
  - **Masaüstü Sürükle-Bırak (`DRAG` Modu)**: Elinizi yumruk yaptığınızda bilgisayar faresinin sol tuşu basılı tutulur ve masaüstündeki pencereleri, dosyaları veya kaydırma çubuklarını sürükleyebilirsiniz.
  - **Sayfa Kaydırma (`SCROLL` Modu)**: `M` tuşuyla `SCROLL` moduna geçerek yumruk yaptığınız elinizi yukarı/aşağı hareket ettirip web sayfalarını veya belgeleri kaydırabilirsiniz.

## Kurulum ve Çalıştırma

```bash
pip install -r requirements.txt
python main.py
```

## Klavye Kısayolları

| Tuş | İşlev |
| :--- | :--- |
| **`B`** | Bonus fare kontrolünü Aç / Kapat |
| **`M`** | Yumruk modunu değiştir (`DRAG`: Sürükle-Bırak / `SCROLL`: Sayfa Kaydırma) |
| **`O`** | Ekrandaki sanal kaydırılabilir nesneleri Göster / Gizle |
| **`R`** | Sanal nesnelerin konumunu sıfırla |
| **`Q` / `ESC`** | Uygulamadan çıkış |

