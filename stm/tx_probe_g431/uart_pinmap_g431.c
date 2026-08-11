/*
 * Карта пинов UART для B-G431B-ESC1.
 *
 * Зачем. Файл варианта PeripheralPins_B_G431B_ESC1.c выводит наружу ровно
 * один UART — USART2 на PB3/PB4, и это VCP встроенного ST-LINK. USART1 на
 * PB6/PB7 из карты вырезан, хотя аппаратно он там есть (см. общий
 * PeripheralPins.c для корпуса G431C: PB_6 — USART1_TX, PB_7 — USART1_RX).
 * Без этого файла HardwareSerial(PB7, PB6) не найдёт периферию и канал к
 * ESP32 молча не поднимется.
 *
 * Почему отдельным .c, а не в скетче. Карты в варианте объявлены слабыми
 * символами и определены в файле на C. Сильное определение с тем же именем
 * перекрывает слабое только при совпадении компоновки, а .ino компилируется
 * как C++ — имя было бы искажено, и перекрытия не произошло бы. Отсюда и
 * отдельная единица трансляции на C.
 *
 * USART2 в списке ОСТАВЛЕН: он нужен встроенному VCP, и выкинув его отсюда мы
 * потеряли бы отладочный канал, ради которого эту плату отчасти и выбрали.
 */
#include "pins_arduino.h"
#include "PeripheralPins.h"

#ifdef ARDUINO_B_G431B_ESC1

const PinMap PinMap_UART_TX[] = {
  {PB_3, USART2, STM_PIN_DATA(STM_MODE_AF_PP, GPIO_PULLUP, GPIO_AF7_USART2)},
  {PB_6, USART1, STM_PIN_DATA(STM_MODE_AF_PP, GPIO_PULLUP, GPIO_AF7_USART1)},
  {NC,   NP,     0}
};

const PinMap PinMap_UART_RX[] = {
  {PB_4, USART2, STM_PIN_DATA(STM_MODE_AF_PP, GPIO_PULLUP, GPIO_AF7_USART2)},
  {PB_7, USART1, STM_PIN_DATA(STM_MODE_AF_PP, GPIO_PULLUP, GPIO_AF7_USART1)},
  {NC,   NP,     0}
};

#endif
