#include <bits/stdc++.h>
#include <cstring>
#include <iostream>
#include <cstring>

 
int compareStrings(const void* a, const void* b) {
    const char* str1 = static_cast<const char*>(a);
    const char* str2 = static_cast<const char*>(b);
    return std::strcmp(str1, str2);
}

int main() {
     
    const char* str1 = "Hello";
    const char* str2 = "World";

     
    int result = compareStrings(str1, str2);

     
    if (result == 0) {
        std::cout << "The strings are equal." << std::endl;
    } else if (str1 == nullptr || str2 == nullptr) {
        std::cout << "One of the strings is null." << std::endl;
    } else {
        std::cout << "The strings are different. Result: " << result << std::endl;
    }

    return 0;
}
