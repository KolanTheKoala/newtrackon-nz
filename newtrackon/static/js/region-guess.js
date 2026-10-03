// Guess the visitor's "fast from" region from their browser time zone. Same grouping as the server's regions:
// the Middle East, Caucasus, Central Asia and Russia's Asian zones count as Europe; Hawaii and the Galapagos as Americas.
var NT_REGION_NAMES = {
    oceania: 'Australia, New Zealand or the Pacific',
    asia: 'Asia',
    europe: 'Europe, the Middle East or Africa',
    americas: 'the Americas'
};

function ntGuessRegion() {
    var tz = '';
    try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (e) { return ''; }
    var eu = /^Asia\/(Dubai|Muscat|Riyadh|Qatar|Bahrain|Kuwait|Baghdad|Tehran|Jerusalem|Tel_Aviv|Gaza|Hebron|Amman|Beirut|Damascus|Aden|Nicosia|Famagusta|Istanbul|Tbilisi|Yerevan|Baku|Almaty|Qostanay|Qyzylorda|Aqtobe|Aqtau|Atyrau|Oral|Tashkent|Samarkand|Bishkek|Dushanbe|Ashgabat|Yekaterinburg|Omsk|Novosibirsk|Barnaul|Tomsk|Novokuznetsk|Krasnoyarsk|Irkutsk|Chita|Yakutsk|Khandyga|Vladivostok|Ust-Nera|Magadan|Sakhalin|Srednekolymsk|Kamchatka|Anadyr)$/;
    if (eu.test(tz) || /^Indian\/(Mauritius|Reunion|Mayotte|Comoro|Antananarivo|Mahe)$/.test(tz)) { return 'europe'; }
    if (/^Atlantic\/(Bermuda|Stanley|South_Georgia)$/.test(tz)) { return 'americas'; }
    if (/^Pacific\/(Honolulu|Johnston|Midway|Galapagos|Easter)$/.test(tz)) { return 'americas'; }
    if (/^(Australia|Pacific)\//.test(tz)) { return 'oceania'; }
    if (/^(Asia|Indian)\//.test(tz)) { return 'asia'; }
    if (/^(Europe|Africa|Atlantic)\//.test(tz)) { return 'europe'; }
    if (/^America\//.test(tz)) { return 'americas'; }
    return '';
}
